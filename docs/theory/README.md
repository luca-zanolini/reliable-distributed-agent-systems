# Theory of reliable agent systems

A companion to the code: the concepts behind each stage, stated with an intuitive
picture, precise definitions, and propositions with proofs, connected to the files that
implement them and to the literature they come from. It grows with every stage.

## The whole story in one page

A **model** is an oracle behind a counter: text in, text out, no memory, never quite
the same answer twice, and it stops when the page runs out whether or not it has
finished (Chapter 1). To make it useful we put a **runtime** around it: a trusted
program that keeps the conversation, re-sends it on every call, and lets the model
*request* actions. Model and runtime in a loop form an **agent** (Chapter 2). The model
proposes; a **gate** in the runtime disposes, answering every request with a result or a
refusal, so that nothing the model says can cause an effect the rules do not allow, and
**budgets** end every run whether or not the model ever stops.

A runtime can crash. So every decision and every action is written to an append-only
**journal** before it takes effect, and recovery replays the journal (Chapter 3). One
case cannot be resolved by reading: an action announced but not confirmed may or may not
have happened. Actions whose repetition is harmless are repeated; actions a receiver can
deduplicate are repeated under the same key; all others are not repeated, and the
uncertainty is reported.

An agent reads text written by strangers, and some of it will try to steer it. Since the
model cannot be made immune, the runtime bounds what any request can achieve: each role
receives a small, explicit **grant**, denied by default and checked on every request; tool
descriptions are pinned; tools run in a separate process without the host's credentials
(Chapter 4). Injection can change what is asked, never what can succeed.

Put several agents on one task and a new enemy appears: **time**. Two agents can both read
"task free" and both take it; two can edit the same version and one erases the other; a
check can concern yesterday's version while today's is accepted (Chapter 6). The cures are
old ones: a single conditional step for ownership, writes conditional on their base version,
versions named by their contents, and acceptance bound to evidence about that very version.

Spread the agents across processes and machines and the channel itself becomes unreliable
(Chapter 7): a timeout cannot tell a lost request from a lost reply, so retried operations
must be idempotent; a silent worker cannot be told from a slow one, so every failure detector
sometimes accuses the living, and a replaced worker may keep acting, a zombie. Only the
resource it touches can refuse it, which is the job of fencing tokens.

Finally, a nondeterministic system cannot be judged by a single run. It is judged by an
**exam**: fixed tasks, validators that check evidence rather than claims, repeated runs,
immutable records with provenance, traces of every step, and comparisons that account for
sampling noise (Chapter 5).

## Chapters

| Chapter | Core question | Stage | Key results |
|---|---|---|---|
| [1. The model](01-the-model.md) | What is a model call, exactly? | 1 | conversation state is client state; soundness of constrained decoding; shape is not truth |
| [2. The agent](02-the-agent.md) | What is an agent, and what can it do? | 2 | confinement against arbitrary model output; termination without trusting the model; refusal is not termination |
| [3. Durability](03-durability.md) | What survives a crash, and what may be repeated? | 3 | replay reconstructs state; indistinguishability lemma; no exactly-once without the receiver; guarantees by effect class |
| [4. Authority](04-authority.md) | How much can go wrong, and who decides? | 4 | the blast radius is the grant; injection is bounded, not prevented; pinning; per-request limits |
| [5. Evaluation](05-evaluation.md) | How do we know it works? | 5 | evidence versus claims; pass@k versus pass^k; intervals on small samples; sound regrading |
| [6. Coordination](06-coordination.md) | What goes wrong when agents share work? | 6 | check-then-act admits two owners; atomic conditional claims; lost updates and optimistic concurrency; content addressing; version-bound acceptance |
| [7. Processes and networks](07-processes-and-networks.md) | What changes when agents are separate processes? | 7 | lost request ≡ lost reply; no perfect failure detector; zombies are unavoidable, protection belongs at the resource; one serialization point |
| [References](references.md) | | | peer-reviewed sources first; preprints labelled |

Each chapter has the same parts: **the picture** (a story to keep in mind), **definitions**,
**results** with proofs, **common misconceptions**, **in this project** (stage and files),
and a one-paragraph **refresher**.

## Common misconceptions, collected

| Misconception | Correction | Where |
|---|---|---|
| The model remembers the conversation. | The runtime re-sends it every call. | 1.1 |
| A successful response is a complete one. | Only the stop reason says so. | 1.6 |
| A schema makes answers correct. | It fixes shape, not truth. | 1.5 |
| The runtime is the model plus its tools. | The model is outside; agent = model + runtime. | 2.1 |
| A denied request ends the run. | It returns a refusal; the run continues. | 2.3 |
| The model decides what happens. | It decides what is requested; the gate decides what happens. | 2.1 |
| An unsafe action is not retried because its content is unknown. | Its content is journaled; whether it happened is unknown. | 3.3 |
| On recovery, ask the model again. | Recorded decisions are reused, never recomputed. | 3.2 |
| Hiding a tool protects it. | The refusal at the gate protects it. | 4.1 |
| The agent follows the policy. | The runtime enforces it; the model never sees it. | 4.5 |
| Harmless tools make a harmless role. | Checks are per request; grant less. | 4.3 |
| The agent said the tests pass. | That is a claim; the exit code is evidence. | 5.2 |
| 15/15 beats 14/15. | Not at n = 15; the intervals overlap. | 5.2 |
| Agents on different tasks cannot interfere. | Every shared file, list or head is a place to interfere. | 6.1, 6.3 |
| Merging concurrent edits fixes conflicts. | It guarantees convergence, not a correct result. | 6.3 |
| A worker's own journal prevents repeated work. | Not by other workers; that needs conditional shared state. | 6.6 |
| Visible in the logs means safe. | Detection is a comparison; safety is refusing what it reveals. | 6.5 |
| A timeout means the request failed. | It may have been executed; only the reply was missing. | 7.1 |
| A suspected worker is dead. | It may only be slow. | 7.2 |
| Refusing the zombie's report makes it safe. | Only the resource it touched can refuse its action. | 7.3 |
| A signed message is secret. | Authentication is not encryption. | 7.6 |

## Conventions

Definitions, propositions and theorems are numbered per chapter. "Model" always means the
language model; "runtime" the trusted program around it; "agent" their combination.
Distributed-systems notions follow Cachin, Guerraoui and Rodrigues [CGR11]; safety and
liveness follow Lamport [Lam77] and Alpern and Schneider [AS85]. Sources are peer-reviewed
unless labelled otherwise in the [references](references.md).
