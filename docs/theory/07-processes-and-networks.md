# 7. Processes and networks

## The picture

The office closes and everyone works from home. The clerks and the manager now
communicate only by post. Letters can arrive late, or not at all; a stranger could post
a letter pretending to be a clerk; and the manager hears nothing at all from a clerk who
is ill, which is exactly what she hears from a clerk whose letters are stuck in the post.

So the office adopts three habits. Every letter is **signed** with a seal only that clerk
and the manager know. Every clerk posts a short **"still here" card** every few minutes;
a clerk silent for too long is presumed gone and their job is handed to someone else. And
any instruction that might have been lost is **sent again**, so the manager makes sure that
receiving it twice does no harm.

One problem survives all three habits. A clerk presumed gone was only delayed. When she
comes back she finishes her job and posts its result to the customer, not knowing someone
else already did. The manager can refuse her report; she cannot recall a letter that has
already reached the customer.

## Definitions

> **Intuition.** A process is a program running with its own memory. Whatever happens to it, other processes are unaffected except through the messages they exchange.

**Definition 7.1 (Process and isolation).** A process has private state, reachable by other
processes only through messages. A process fails by **crashing** (it stops) or is merely
**slow**; we do not yet consider processes that lie (Byzantine faults return in Stage 10).

> **Intuition.** Messages travel through a channel that can delay them without limit, or drop them, but does not invent them.

**Definition 7.2 (Unreliable channel; timing models).** Messages may be delayed or lost. In
the **asynchronous** model there is no bound on delays or relative speeds; in the
**partially synchronous** model such bounds exist but are unknown, or hold only after some
unknown time [DLS88]. The channel abstractions of [CGR11] (fair-loss, stubborn, perfect
links) are built on top by retransmission and duplicate suppression.

> **Intuition.** A remote call is a letter asking for something and a letter answering. Either letter can go missing, and the asker cannot tell which.

**Definition 7.3 (Remote procedure call and its semantics).** A client sends a request and
waits for a reply up to a timeout. A call has **at-least-once** semantics if the client
retries until it receives a reply, and **at-most-once** if the server suppresses duplicates
of a request it has already executed [BN84].

> **Intuition.** A seal on a letter proves who wrote it and that nobody changed it, as long as only the writer and the reader know how to make the seal.

**Definition 7.4 (Message authentication).** With a secret key $k$ shared by a worker and the
coordinator, a message $m$ is sent with a tag $t = \mathrm{MAC}_k(m)$, here HMAC [BCK96]. The
receiver accepts $m$ only if $t$ verifies under the sender's key. Including a timestamp in
$m$ and rejecting old timestamps limits replays to a window.

> **Intuition.** A device that tells you, for each colleague, whether you believe they have stopped working, and that may be wrong.

**Definition 7.5 (Failure detector).** A module that outputs a set of *suspected* processes
[CT96]. It is **complete** if every crashed process is eventually suspected forever, and
**accurate** to a degree: *strongly* accurate if no correct process is ever suspected,
*eventually strongly* accurate if there is a time after which no correct process is
suspected. Complete and strongly accurate is the **perfect** detector $\mathcal{P}$; complete
and eventually strongly accurate is the **eventually perfect** detector $\Diamond\mathcal{P}$.

> **Intuition.** "Nothing heard for a while, so presume them gone."

**Definition 7.6 (Heartbeat detector).** Each process sends a heartbeat every $\Delta$; the
observer suspects a process from which nothing has been heard for longer than a timeout
$T > \Delta$, and stops suspecting it when it is heard again.

> **Intuition.** Someone presumed gone, replaced, and still acting.

**Definition 7.7 (Zombie).** A process that was suspected, whose task was reassigned, and that
continues to act on that task.

## Results

> **Intuition.** If the answer did not come, you cannot tell whether your question was lost or the answer was; so you ask again, and the other side must treat a repeated question as harmless.

**Proposition 7.1 (Timeouts cannot distinguish a lost request from a lost reply).** Consider
an execution in which a request is lost and one in which the request is executed and its
reply is lost. Up to the timeout, the client's observations are identical. Hence a client
that must eventually obtain the effect has to retry, and correctness requires the server's
handling of the request to be idempotent.

*Proof.* In both executions the client sends the same message and receives nothing before
the timeout; its view is the same. If it does not retry, the first execution never obtains
the effect; if it retries, the second executes the request twice. Only idempotent handling
makes the retry safe in both. $\square$

The argument has the same shape as Lemma 3.3: two executions that look identical to the
party that must decide.

> **Intuition.** A colleague who is merely slow looks exactly like one who has stopped, for as long as you care to wait. So any rule that eventually notices the stopped ones will sometimes wrongly accuse a slow one.

**Proposition 7.2 (No perfect failure detector in asynchronous systems).** In the asynchronous
model, no implementation of a failure detector is both complete and strongly accurate.

*Proof.* Suppose a complete detector suspects a crashed process $p$ at some time $t$ in an
execution $E$. Build $E'$ identical to $E$ except that $p$ is correct and all its messages are
delayed beyond $t$; asynchrony permits this. Up to $t$ the observer cannot distinguish $E'$
from $E$, so it suspects $p$ at $t$ in $E'$ too, although $p$ is correct. Strong accuracy is
violated. $\square$

Under partial synchrony, a heartbeat detector whose timeout increases after each false
suspicion is eventually perfect [CT96, DLS88]. The detector in this stage uses a fixed timeout,
which is practical but carries no such guarantee: if delays exceed the timeout forever, it keeps
being wrong.

> **Intuition.** Since mistakes about who has stopped are unavoidable, someone will sometimes be replaced while still working. The only place that can always refuse their late work is the thing their work touches.

**Proposition 7.3 (Zombies are unavoidable; protection belongs at the resource).** If tasks are
reassigned on suspicion, then in some executions two processes act on the same task. Safety of
a resource therefore requires the resource itself to reject actions from stale owners.

*Proof.* By Proposition 7.2 there are executions in which a correct, slow owner is suspected;
its task is reassigned; the slow owner then continues, and both act. A coordinator that refuses
the stale owner's report protects only the coordinator's records; an action the stale owner
performed directly on another resource reaches that resource unchecked unless the resource
checks a token of current ownership. $\square$

This is the end-to-end argument [SRC84]: a guarantee about the effect on a resource can only be
fully enforced at that resource. The token is a **fencing token**: a number increased on every
reassignment and checked by the resource, which rejects any request carrying an older one. Leases
[GC89] bound how long an owner may act without renewing; fencing makes acting after expiry harmless.

> **Intuition.** If the manager deals with one letter at a time, then "is the job free?" and "give it to me" can never be split by someone else's request.

**Proposition 7.4 (A single serialization point makes requests atomic).** If the coordinator
applies each request's check and update under one lock, its operations on shared state are
linearizable [HW90]; in particular, a claim is a compare-and-set, and ownership is exclusive
(Proposition 6.2) except when a false suspicion reassigns a task (Proposition 7.3).

*Proof.* The lock orders all critical sections; each request's check and update occur within one
section, so every execution is equivalent to one in which requests occur one at a time in that
order, consistent with real time. $\square$

> **Intuition.** When requests fail, wait before retrying, wait longer each time, and add randomness, so that many clients do not all retry at the same moment.

**Remark 7.5 (Backoff, and its limits).** Exponential backoff dates from Ethernet's collision
handling [MB76]; random jitter desynchronizes clients that failed together. Retries recover from
intermittent loss, not from a server that is persistently slower than the client's timeout: then
every retry fails and only adds load, and a client must survive the exhaustion of its retries.

> **Intuition.** A seal proves who wrote a letter and that nobody altered it. It does not hide what the letter says, and it does not stop someone from posting a copy of it again soon after.

**Remark 7.6 (What authentication does and does not give).** A verified tag establishes origin and
integrity. It does not provide confidentiality (that requires encryption, for example TLS; mutual
TLS additionally authenticates the server to the client), and a timestamp window bounds but does
not prevent replay (a nonce does).

## Common misconceptions

- *"A timeout means the request failed."* It means no reply arrived in time; the request may have
  been executed (Proposition 7.1).
- *"A suspected worker is dead."* It may only be slow (Proposition 7.2).
- *"If the coordinator rejects the zombie's report, the system is safe."* Only the coordinator's
  records are; resources the zombie touched directly need their own check (Proposition 7.3).
- *"Retries make a call reliable."* Only against intermittent loss, and only with idempotent
  handlers (Proposition 7.1, Remark 7.5).
- *"A signed message is a secret message."* Authentication is not encryption (Remark 7.6).

## In this project (Stage 7, [`07-distributed-workers/`](../../07-distributed-workers/))

- `protocol.py`: Definition 7.4 (HMAC-SHA256, replay window) and versioned, schema-validated
  messages.
- `coordinator_service.py`: Proposition 7.4 (one lock per request), the heartbeat detector of
  Definition 7.6, idempotent handling of retried completions (Proposition 7.1), refusal of
  reports from non-owners.
- `rpc.py`: at-least-once calls with exponential backoff and jitter (Remark 7.5).
- `experiments.py`: a killed worker detected in 0.7 s and its job recovered; a frozen worker
  falsely suspected, revised when it resumed, its report refused, and its external effect
  duplicated: the zombie of Proposition 7.3, which motivates fencing tokens in the next stage.

## Refresher

Separate processes fail separately and talk through a channel that delays and loses. A
timeout cannot tell a lost request from a lost reply, so retried operations must be
idempotent. A silent process cannot be told apart from a slow one, so every failure detector
sometimes accuses the living, and reassigned work can then be done twice: the zombie. The
coordinator can refuse a zombie's report; only the resource the zombie touches can refuse its
action, which is what fencing tokens are for. Sign messages to know who sent them, version
them so incompatibilities are explicit, and validate them before they touch state.

*References:* [CGR11] [DLS88] [BN84] [BCK96] [CT96] [SRC84] [GC89] [HW90] [MB76]. Full list: [references.md](references.md).
