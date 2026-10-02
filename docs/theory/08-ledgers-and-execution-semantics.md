# 8. Ledgers and execution semantics

## The picture

The office from Chapter 7 now keeps a **ledger**: a bound book in which every job has a
page. Nothing counts unless it is written in the book, and the book is written in ink:
lines are added, never erased. When a clerk takes a job, the manager writes the clerk's
name on its page together with a **number**, one higher than any number ever written on
that page, and gives the clerk a slip with the number on it. From then on the manager
accepts news about that job only from someone holding the slip with the current number.
A clerk who was presumed gone comes back with an old slip; the manager compares numbers
and turns her away.

Letters to customers are no longer written by clerks at all. A finished draft goes to a
checker, and only an approved draft goes to the **post room**. Before the post room
sends a letter it writes "about to send" on the page; after the post office confirms,
it writes "sent". If the post room burns down in between, the page says "about to send",
and the new post room sends the letter again, with the job's number on the envelope.
The post office keeps a list of every number it has delivered, and quietly discards
the second envelope.

## Definitions

> **Intuition.** A ledger is the one place where the truth about every task is kept. It only ever grows, so it can always be read back.

**Definition 8.1 (Ledger).** A ledger is a process holding, for each task, a state from a
finite set $S$ and a log $L$ of events. Its state is a deterministic function of the log,
$\sigma = \mathrm{fold}(\delta, \sigma_0, L)$, with $\delta$ the transition function of
Definition 8.2; the log is append-only and stored durably [GR93, Moh92]. A request is
*accepted* when the event it causes has been appended and forced to stable storage.

> **Intuition.** A task can only move along arrows somebody drew in advance. Every other move is not an error to handle; it is impossible.

**Definition 8.2 (Task lifecycle).** $S = \{\mathsf{OPEN}, \mathsf{IN\_PROGRESS},
\mathsf{UNDER\_CHECK}, \mathsf{CHECKING}, \mathsf{APPROVED}, \mathsf{SENDING},
\mathsf{SENT}, \mathsf{FAILED}\}$, with $\mathsf{SENT}$ and $\mathsf{FAILED}$ final, and
$\delta$ partial: defined only on the edges claim, submit, claim-check, approve, reject,
expire, give-up, sending, sent. An event outside the domain of $\delta$ is never applied.
A task is *held* in $\mathsf{IN\_PROGRESS}$ and $\mathsf{CHECKING}$.

> **Intuition.** Taking a task gives you two things: a time limit, and a numbered slip. The time limit says when the task can be given to someone else; the slip says whether your news is still current.

**Definition 8.3 (Lease, epoch, token).** Each task carries an *epoch* $e \in \mathbb{N}$,
initially $0$. A claim of an unheld task sets $e \leftarrow e + 1$, records the claimer as
*holder*, and returns the token $\tau = e$ together with a *lease*: a duration after which,
measured on the ledger's clock and unless renewed, the task stops being held [GC89]. A
request about a task carrying token $\tau$ from process $p$ is *current* iff the task is held,
its epoch is $\tau$ and its holder is $p$. A non-current request is answered STALE and has no
effect. The token is a *fencing token* [Bur06, Kle17].

> **Intuition.** Repeating a request should do nothing new; to make that possible, a repeat must be recognisable as a repeat.

**Definition 8.4 (Idempotency key; effectively-once).** An *idempotency key* is a value $k$
carried by every attempt to cause one effect. A receiver *deduplicates* on $k$ if it performs
the effect for the first attempt carrying $k$ and answers every later attempt with the
recorded outcome, without performing it again [BN84]. An effect is delivered
*effectively once* if it is attempted at least once and the receiver deduplicates.

> **Intuition.** Before doing something that cannot be undone, write down that you are about to do it. Then, after a crash, you know exactly where the uncertainty is.

**Definition 8.5 (Intent).** For an irreversible effect $x$ of task $t$, the ledger appends an
intent event (here $t \to \mathsf{SENDING}$) before $x$ is attempted, and a completion event
($t \to \mathsf{SENT}$) after $x$ is confirmed.

## Results

> **Intuition.** If the number handed out was not written down first, a crash can make the ledger hand the same number to two people.

**Lemma 8.1 (Save before reply).** If every accepted request is answered only after its event
is durable, then after any crash and recovery the ledger's epochs are at least as large as
every token any process holds.

*Proof.* A process holds token $\tau$ for task $t$ only if it received a claim reply carrying
$\tau$. By hypothesis the event setting $t$'s epoch to $\tau$ was durable before that reply
was sent, so it survives the crash; replay reconstructs epoch $\ge \tau$, as epochs never
decrease under $\delta$. $\square$

In the other order a reply can escape before its event is durable: after a crash the epoch
is $\tau - 1$, the next claim issues $\tau$ again, and two processes hold the same token.
Gaps in the numbering (an event saved, its reply lost) are harmless: only the order matters.

> **Intuition.** Everyone who is not the current holder has an old slip, so their news is turned away, however late it arrives and however wrong the clocks are.

**Theorem 8.2 (Fencing).** Under Lemma 8.1, at every point of every execution at most one
process can issue a current request about a task, and no request carrying a token smaller
than the task's epoch ever changes the ledger's state. The guarantee does not depend on clock
synchronisation or on any bound on message delay.

*Proof.* Currency requires token $=$ epoch and holder $=$ the requester. The epoch has exactly
one holder at a time (Definition 8.3: the claim that set it named one), and only a claim
changes the epoch, strictly increasing it (Lemma 8.1 ensures this across crashes). A token
$\tau <$ epoch therefore fails the equality whenever it arrives. Time enters only through
expiry, which removes the holder without changing the epoch, so a mistaken expiry can make a
current holder stale but can never make a stale one current. $\square$

Leases thus affect only liveness (how soon a dead holder's task is reassigned, how often a
slow one is wrongly fenced); safety rests on the token alone. This is why the ledger may give
every held task a fresh lease after a restart, and why the two machines' clocks need not
agree. The idea is the "sequencer" of Chubby [Bur06]; the name *fencing token* follows
[Kle17].

> **Intuition.** Writing "sent" and actually sending happen in two different places. No order of the two survives every crash: one order risks a duplicate, the other a loss.

**Proposition 8.3 (No ordering of record and effect is crash-safe on its own).** Let $x$ be an
irreversible effect at a receiver that does not deduplicate, and $r$ the ledger's record of it.
If the process performing them can crash between the two steps, then performing $x$ before
writing $r$ admits an execution with $x$ performed twice, and writing $r$ before $x$ admits an
execution in which $x$ is never performed.

*Proof.* Effect first: crash after $x$, before $r$. Recovery sees no record and, indistinguishably
from the execution in which the crash preceded $x$ (Lemma 3.3), must perform $x$ to guarantee it;
in this execution that is the second time. Record first: crash after $r$, before $x$. Recovery sees
the record and, indistinguishably from the execution in which $x$ completed, must not repeat $x$;
in this execution $x$ never happens. $\square$

> **Intuition.** So keep trying until you hear back, and let the receiver throw away repeats. Retrying makes sure the email arrives; the receiver's memory makes sure it arrives once.

**Theorem 8.4 (Effectively-once delivery).** With an intent recorded before the effect
(Definition 8.5), a sender that retries every task in $\mathsf{SENDING}$ until the receiver
answers, with the task's identifier as idempotency key, and a receiver that deduplicates
durably, every approved task's effect is performed exactly once at the receiver, provided the
sender and receiver eventually run long enough to complete an exchange.

*Proof.* At most once: every attempt for task $t$ carries key $t$, and the receiver performs the
effect only for the first attempt with that key; its record of delivered keys is durable, so this
holds across receiver crashes. At least once: an approved task gets its intent before any attempt;
a task stays in $\mathsf{SENDING}$ until a receiver answer has been recorded, and every recovered
sender retries it; by assumption some attempt completes, and the first completed attempt performs
the effect or finds it already performed. Finally, one approval per task means one key per
effect, and the sender sends the approved text recorded in the ledger, so all attempts describe
the same effect. $\square$

"Exactly once" is thus an end-to-end property: it holds at the receiver because the receiver
enforces it, not because any message was delivered exactly once [SRC84]. Remove the
receiver's memory and the guarantee is gone, as the counterfactual experiment shows.

> **Intuition.** A message sent twice must be recognised by the one who receives it; then answering it twice is harmless.

**Remark 8.5 (Recognising repeats on every edge).** The same construction protects requests to
the ledger itself. A retried submit carrying the token and draft already recorded is answered
"already accepted" rather than refused; a retried claim carrying the same request identifier is
answered with the original task and token rather than granting a second one, which would leave
the first, recorded but unannounced, as an orphan until its lease expires. Each is an instance
of Definition 8.4 with the ledger as receiver.

## Common misconceptions

- *"A lease guarantees there is only one owner."* Only if clocks and delays are bounded; a
  frozen holder outlives its lease without knowing. The token, checked where the action lands,
  is what guarantees it (Theorem 8.2).
- *"Fencing needs synchronised clocks."* It needs none; clocks only affect how often good
  holders are fenced (Theorem 8.2).
- *"Write the record first and the effect cannot be duplicated."* It can instead be lost
  (Proposition 8.3).
- *"Our messaging layer gives exactly-once delivery."* No transport can; exactly-once is built
  from at-least-once attempts and a receiver that deduplicates (Theorem 8.4).
- *"An idempotency key should identify the request."* It must identify the *effect*: one email,
  one key, however many attempts and drafts preceded it.
- *"Restart the ledger and reset the counters; they are just numbers."* A counter that goes
  back makes old tokens current again (Lemma 8.1).

## In this project (Stage 8, [`08-reliable-task-ledger/`](../../08-reliable-task-ledger/))

- `ledger.py`: Definitions 8.1–8.3 and 8.5. A checksummed append-only journal forced to disk
  before every reply (Lemma 8.1); state as a fold; a partial transition table; per-task epochs;
  leases on the ledger's monotonic clock, fresh after a restart; repeats recognised (Remark 8.5).
- `sender.py` and `mail_service.py`: Theorem 8.4. The intent, the pinned approved text, the task
  identifier as key, retries until an answer, durable deduplication at the receiver.
- `lab.py`: workers in containers with no network route to the mail service: only the sender can
  cause the effect, so fencing protects the ledger and idempotency protects the world.
- `experiments.py`: the zombie fenced (Theorem 8.2), the ledger crashing between save and reply
  with no orphan (Lemma 8.1, Remark 8.5), the sender crashing after delivery and a mail timeout
  with one delivery each (Theorem 8.4), and the counterfactual without receiver memory: a duplicate
  (Proposition 8.3).

## Refresher

A ledger is an append-only log and the state folded from it; nothing counts until it is on disk,
and nobody hears of it before that. Every claim increments the task's epoch and hands it out as a
token; only the current holder's token is accepted, so a zombie's late news is refused however
late it comes and whatever the clocks say. Leases decide when a task moves, tokens decide whose
news counts. An irreversible effect cannot be made atomic with its record: record an intent,
retry until answered, and let the receiver deduplicate on a key that names the effect. That is
what "exactly once" means.

*References:* [GR93] [Moh92] [GC89] [Bur06] [Kle17] [BN84] [SRC84]. Full list: [references.md](references.md).
