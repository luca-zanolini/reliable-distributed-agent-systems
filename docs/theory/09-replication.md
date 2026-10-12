# 9. Replication

## The picture

The ledger from Chapter 8 was one bound book in one office. If the office burned, the
truth burned with it. Now the book is kept in **three offices**, identical copies kept in
step by a strict procedure: a new line counts only once **two of the three** offices have
written it, and nobody is told anything about that line before then. If one office burns,
the other two still have every line that ever counted, and they agree on which office now
leads.

The managers who take clerks' requests no longer keep a book of their own. A manager reads
the page, decides by the rules, and sends the new page to the offices with a condition:
*"replace this page only if it is still the one I read."* Two managers who read the same
page at the same moment cannot both win: the offices apply the first, and the second one's
condition no longer holds. Time-keeping stays in one place: the leading office runs the
clocks, and when a clock runs out it writes that down like any other line. When the
leadership moves, every clock starts again.

## Definitions

> **Intuition.** A store that behaves like one careful machine, even though it is several, as long as most of them are up.

**Definition 9.1 (Replicated store).** A key-value store implemented by state-machine
replication [Sch90] over $n = 2f + 1$ members with a consensus log [OO14]: a write is
*committed* once a majority has appended it, and only then acknowledged. The store is
*linearizable* [HW90]: every operation appears to take effect at one instant between its
invocation and its response, consistent with real-time order. It is available while a
majority of members can communicate.

> **Intuition.** "Change this, but only if nobody changed it since I looked."

**Definition 9.2 (Conditional write).** A transaction $\langle C, W\rangle$ applies the writes
$W$ atomically if every comparison in $C$ holds at the instant it is applied, and nothing
otherwise. Comparing a key's modification revision with the one read turns a read, a
decision and a write into one optimistic step [KR81].

> **Intuition.** The front-end knows the rules but remembers nothing; the store remembers everything but knows no rules.

**Definition 9.3 (Front-end).** A stateless, trusted process that turns a participant's
request into at most one conditional write: it reads the task, applies the transition
function $\delta$ of Definition 8.2, and writes the result conditional on the version read.
Participants hold no credentials to the store; front-ends do.

> **Intuition.** A store lease is a timer kept by the store itself: stop renewing and the keys attached to it disappear, as one recorded change.

**Definition 9.4 (Store lease).** A lease with a time to live, granted by the store, renewed by
keep-alives; keys may be attached to it. When it lapses (as measured by the store's current
leader), its keys are deleted by a committed write. When leadership changes, every lease is
renewed in full.

## Results

> **Intuition.** Nobody hears about a line until it can no longer be lost.

**Lemma 9.1 (Commit before reply).** If a front-end answers a request only after the store has
acknowledged its write, then after any failure of at most $f$ members and any number of
front-ends, every token held by a participant is at most the epoch recorded in the store.

*Proof.* A participant holds token $\tau$ for task $t$ only if it received a claim reply carrying
$\tau$. That reply followed the acknowledgment of a write setting $t$'s epoch to $\tau$; an
acknowledged write is committed on a majority, and a committed entry is present in the log of
every later leader (leader completeness [OO14]), so it survives any $f$ member failures.
Front-ends hold no state, so their failures lose nothing. Epochs never decrease under
$\delta$. $\square$

This is Lemma 8.1 with "durable" read as "committed on a majority". A write that is committed
but whose acknowledgment is lost is harmless in the same way a gap in the numbering was:
the participant never received that token.

> **Intuition.** Two managers who read the same page cannot both replace it.

**Theorem 9.2 (Unique holders across front-ends).** If every claim is a conditional write on the
task's version (Definition 9.2) and Lemma 9.1 holds, then for every task and every epoch $e$, at
most one participant is ever told it holds the task with token $e$, however many front-ends
serve claims concurrently and whatever members and front-ends fail.

*Proof.* Every write that sets a task's epoch to $e$ is conditional on the version in which the
epoch was $e - 1$. By linearizability the store applies conditional writes one at a time; after
the first succeeds, the version has changed, so every other write conditional on the same version
fails, and its front-end does not reply with a grant (it re-reads, and the task is no longer
claimable at epoch $e - 1$). A later grant needs a later version, hence epoch $\ge e + 1$. With
Lemma 9.1, no failure brings back a lower epoch. $\square$

> **Intuition.** Without the condition, two managers each copy the old page, each writes "mine, number 5", and the offices keep whichever arrived last. Both clerks walk away with slip 5.

**Proposition 9.3 (Blind writes lose updates).** If claims write unconditionally, there is an
execution in which two participants are told they hold the same task with the same token.

*Proof.* Let two front-ends read task $t$ at version $v$ with epoch $e - 1$ and both compute the
claim with epoch $e$; both unconditional writes succeed (the second overwrites the first), and
both front-ends reply with token $e$. $\square$

The counterfactual experiment found exactly this: six simultaneous claims for three tasks,
through two front-ends, all six told they hold the first task with token 1.

> **Intuition.** A leader who cannot reach a majority cannot make anything count, so it cannot hand out anything.

**Proposition 9.4 (A minority decides nothing).** While a set of members smaller than a majority
is cut off from the rest, no write proposed through it is committed; hence, under Lemma 9.1, no
participant receives a token through it. The system's liveness requires a communicating majority.

*Proof.* Commitment requires acknowledgment by a majority, and two majorities intersect, so a
minority cannot assemble one. Without commitment there is no acknowledgment and no reply. $\square$

> **Intuition.** The slip still decides whose news counts; the clocks only decide when a task moves. Moving the clocks to another office changes nothing about the slips.

**Theorem 9.5 (Fencing survives replication).** Theorem 8.2 holds for the replicated ledger, with
the same independence from clocks and message delays, provided that every expiry is applied as a
conditional write on the task's version and on the absence of a live hold.

*Proof.* Currency of a request is decided by comparing its token with the epoch read in the same
linearizable step that applies its effect (Definition 9.2), so the argument of Theorem 8.2 applies
verbatim. Expiry removes the holder without changing the epoch, and only when the task is still
held under the epoch that was read and no hold remains; an expiry triggered by a lease that
lapsed before a newer claim therefore fails its condition and has no effect. Lease renewal at a
change of leadership, and any imprecision of the store's timing, only delay or advance expiry,
which affects liveness alone. $\square$

> **Intuition.** A repeated request must be recognisable wherever it arrives, so its record must live where every front-end can see it.

**Remark 9.6 (Repeats across front-ends).** Remark 8.5 extends to several front-ends by writing the
record of a claim's request identifier in the same transaction as the claim. A retry, through any
front-end, finds the record and receives the same task and token. A subtle case: a committed write
whose acknowledgment was lost may be resent by the store client and then fail its condition;
a front-end must look for its own record **before** treating the failure as a lost race
(otherwise it would release the lease of the claim that did succeed).

> **Intuition.** A stale read can waste work, but it cannot cause a wrong effect, because every effect goes through a conditional write.

**Remark 9.7 (Reads are hints).** A front-end may read stale state only if it serves reads without
consulting a majority; etcd's default reads are linearizable. Even a stale read cannot violate
safety in this design, because no decision with an effect is taken on a read alone: it is taken
by a conditional write, which is evaluated against the current state. Linearizable reads need
either a round to a majority before answering (ReadIndex [Ong14]) or a leader lease, which makes
safety depend on bounded clock drift.

> **Intuition.** Merging is for things that may happen independently. Owning is not one of them.

**Proposition 9.8 (Mutual exclusion cannot be merged).** No replicated object in which replicas
accept updates without coordination and later converge (a CRDT [SPBZ11]) can guarantee that at
most one participant is ever told it owns a task.

*Proof.* Let two replicas, before exchanging any update, each receive a claim of the same unowned
task. A replica that accepts without coordination must grant its claim (it cannot distinguish
this execution from one in which the other claim never existed), so both participants are told
they own the task; convergence afterwards cannot retract either answer. $\square$

So the claim path needs agreement before answering, which is consensus; CRDTs belong to state
whose concurrent updates commute and whose answers need no exclusivity (Stage 9 has none).

## Common misconceptions

- *"Replicating the ledger means each replica runs the ledger's rules."* Here the store replicates
  generic state, and the rules run in stateless front-ends; what makes them safe is the condition
  on every write (Theorem 9.2).
- *"With a replicated store, writes are safe."* Only conditional ones. Blind read-modify-write
  through two front-ends loses updates, even on a perfectly linearizable store (Proposition 9.3).
- *"Lease deadlines should be replicated."* Deadlines read against each member's own clock would
  make replicas disagree. Time is measured in one place and its effect is recorded as a write.
- *"After a failover the new leader must restore the old leases' remaining time."* It cannot know
  it, and need not: fresh leases cost only liveness (Theorem 9.5).
- *"A CRDT would make claims available during a partition."* It would make them answer; two
  owners would be the answer (Proposition 9.8).
- *"Front-ends are a single point of failure."* They are stateless and interchangeable; but they
  are trusted, which is a different matter (Stage 10).
- *"Raft tolerates a lying member."* Raft tolerates crashes and lost messages, not arbitrary
  behaviour.

## In this project (Stage 9, [`09-replicated-ledger/`](../../09-replicated-ledger/))

- `etcd.py`: Definition 9.1–9.2 and 9.4 as used: linearizable reads, conditional transactions,
  leases with attached keys, watches; failover between members.
- `ledger.py`: $\delta$ of Definition 8.2 as pure functions (task in, task out).
- `frontend.py`: Definition 9.3. Claims as conditional writes with the request record and the hold
  in the same transaction (Theorem 9.2, Remark 9.6), replies after acknowledgment (Lemma 9.1),
  renewals as keep-alives, expiry as a conditional write on epoch and absent hold (Theorem 9.5),
  the SENDING intent unchanged (Theorem 8.4), blind writes for the counterfactual.
- `test_ledger.py`: races, lost acknowledgments and lease lapses against an in-memory store.
- `experiments.py`: leader crash (Lemma 9.1), majority loss (Proposition 9.4), front-end crash between
  commit and reply (Remark 9.6), racing front-ends (Theorem 9.2) and the counterfactual
  (Proposition 9.3), zombie and dead worker across a leader change (Theorem 9.5), intent across a
  leader crash (Theorem 8.4).

## Refresher

Keep the ledger's state in a store replicated on a majority, and never answer before the majority
has it: then no failure of a minority can take back a token. Let stateless front-ends apply the
rules, and make every change conditional on the version they read: then two front-ends can never
hand out the same token, while blind writes would. Keep time in one place, record its effects as
writes, and keep fencing on epochs: then leases, failovers and imperfect clocks only change how
soon work moves. Ownership cannot be merged after the fact, so it needs consensus; the trust placed
in the front-ends and the store's members is what remains to be removed.

*References:* [Sch90] [OO14] [Ong14] [HW90] [KR81] [SPBZ11]. Full list: [references.md](references.md).
