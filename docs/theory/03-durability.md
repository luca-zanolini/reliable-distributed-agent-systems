# 3. Durability and recovery

## The picture

The clerk of Chapter 2 keeps everything in their head. If they faint mid-task and a
replacement arrives, the replacement knows nothing: not the objective, not what was
spent, and, worst of all, not which errands were already run. Told to "just start
over", the replacement sends the same letter twice.

So the office gets a **logbook**, bound, written in ink, never erased. Before running
any errand, the clerk writes *"about to post the letter to on-call"*; after it, *"posted;
receipt attached"*. A replacement reads the logbook from the start and knows exactly
where things stand, with one uncomfortable exception: an entry *"about to post…"* with
nothing after it. Did the clerk faint before reaching the post box, or after? The
logbook cannot say. What the replacement does next depends on what kind of errand it
was.

## Definitions

We use the **crash-recovery** model [CGR11]: a process may crash, losing its volatile
state, and later recover with access to **stable storage** that survives crashes.

> **Intuition.** A bound logbook written in ink: entries are only ever added, and once an entry is finished it survives a faint. At worst, the entry being written at the moment of fainting is half-legible, and it is ignored.

**Definition 3.1 (Journal).** A journal is an append-only sequence of records on
stable storage. $\mathsf{append}(\ell)$ returns only after $\ell$ is durable. After
any crash, the journal is a prefix of the sequence of appended records, possibly
followed by one incomplete record (a torn write), which recovery discards.

> **Intuition.** Everything the office knows is what you learn by reading the logbook from the first page to the last. Memory is just a faster copy of that reading.

**Definition 3.2 (State as a fold).** The run's state is
$s = \mathrm{fold}(\mathsf{apply}, s_0, L)$ for a deterministic transition function
$\mathsf{apply}$ and journal $L$. Live operation appends a record, then applies it;
recovery applies the durable records in order.

> **Intuition.** Before doing anything to the world, write down that you are about to do it; after doing it, write down that it is done.

**Definition 3.3 (Intent before action).** For every permitted tool call $c$, the
runtime performs $\mathsf{append}(\mathsf{intent}(c))$, then executes $c$, then
$\mathsf{append}(\mathsf{result}(c))$. Recording before acting is the write-ahead
rule of database recovery [Moh92, GR93] applied to actions in the world.

> **Intuition.** Actions differ in what happens if they are done twice: a read changes nothing, an overwrite ends in the same place, a keyed request is ignored the second time by a receiver that remembers keys, and anything else simply happens twice.

**Definition 3.4 (Effect classes).** Let $w$ be the state of the world outside the
runtime and $f$ the effect of a call on it.

| Class | Condition | Example |
|---|---|---|
| read | $f(w) = w$ | reading a file |
| idempotent | $f(f(w)) = f(w)$ | overwriting a file with given content |
| keyed | the receiver applies $f$ at most once per idempotency key $k$, remembering the keys it has processed | a service that deduplicates requests by key |
| unsafe | none of the above | appending a line to a log |

> **Intuition.** Three possible promises about how many times an action happens: never more than once, at least once, or exactly once.

**Definition 3.5 (Execution semantics).** A call has *at-most-once* semantics if its
effect occurs zero or one times, *at-least-once* if one or more, and *exactly-once* if
exactly one. The distinction and duplicate suppression by call identifiers are
classical in remote procedure call [BN84].

## Results

> **Intuition.** If reading the logbook always leads to the same conclusions, a replacement clerk ends up knowing exactly what the fainted clerk had written down.

**Proposition 3.1 (Replay reconstructs the state).** If $\mathsf{apply}$ is
deterministic and the same function is used live and on recovery, the recovered state
equals the state the crashed process had after its last durable record.

*Proof.* Both are $\mathrm{fold}(\mathsf{apply}, s_0, L)$ over the same durable prefix
$L$, by induction on $|L|$. $\square$

> **Intuition.** The model's decisions are coin flips that already happened. On recovery, read the recorded outcome instead of flipping again, because the logbook already contains the consequences of the first flip.

**Proposition 3.2 (Record decisions, do not recompute them).** Recovery must reuse
recorded model outputs rather than request them again.

*Argument.* A model call is a sample from a distribution that is in general not a point
mass (Chapter 1). A new sample may request different calls than the recorded one,
while the journal already holds intents and results of the recorded calls: the
recovered history would then contain consequences of a decision it no longer contains.
This is the state-machine replication requirement [Sch90] that replicas be
deterministic: a nondeterministic choice is made once, logged, and thereafter treated
as input. Reuse also avoids paying for the decision twice.

> **Intuition.** "Fainted just before posting the letter" and "fainted just after" leave the same last line in the logbook: "about to post". Nobody reading the book can tell them apart.

**Lemma 3.3 (Indistinguishability).** Consider two executions that agree up to
$\mathsf{append}(\mathsf{intent}(c))$ and differ only in whether the crash occurs
immediately before or immediately after the execution of $c$. After the crash, their
journals are identical.

*Proof.* In both executions nothing is appended between
$\mathsf{append}(\mathsf{intent}(c))$ and $\mathsf{append}(\mathsf{result}(c))$, and
the crash precedes the latter. $\square$

> **Intuition.** Because the two situations look identical, whatever the replacement decides is wrong in one of them: posting again duplicates the letter in one world, not posting loses it in the other. Only the receiver, by recognising a repeat, can break the tie.

**Theorem 3.4 (No exactly-once without the receiver).** No recovery procedure whose
decisions are a function of the journal can guarantee exactly-once execution of an
unsafe effect when crashes may occur during its execution.

*Proof.* By Lemma 3.3 the procedure takes the same decision in both executions. If it
re-executes $c$, the execution that crashed after the effect performs it twice; if it
does not, the execution that crashed before the effect performs it zero times. Either
way one of the two executions violates exactly-once. $\square$

The escape is to move part of the problem to the receiver: if the receiver remembers
the keys it has processed, re-execution is harmless (keyed class). Otherwise one must
choose between at-most-once and at-least-once.

> **Intuition.** The recovery rule, case by case: finished actions are never redone; actions whose repetition is harmless are redone; keyed ones are redone under the same key; unprotected ones are never redone, accepting a possible loss to rule out duplication.

**Theorem 3.5 (Guarantees of recovery by effect class).** Under intent-before-action,
with any finite number of crashes, a recovery procedure that (a) reuses recorded
results, (b) re-executes calls without an intent normally, and (c) for a call with an
intent and no result re-executes read, idempotent and keyed calls (keyed ones with the
same key) and never re-executes unsafe ones, ensures:

1. no call with a recorded result is executed again;
2. read and idempotent calls leave the world as a single execution would;
3. keyed calls take effect at most once, and exactly once if the run is resumed until
   the call has a result;
4. unsafe calls take effect at most once.

*Proof.* Case analysis on the journal state of each call at recovery. With a result,
(a) prevents re-execution: item 1. Without an intent, the call has not started
(intent precedes execution), so running it normally is its first execution. With an
intent and no result, the call executed zero or one times. Read: executions do not
change $w$. Idempotent: $f^j(w) = f(w)$ for all $j \ge 1$, and the re-execution
guarantees $j \ge 1$: item 2. Keyed: every execution carries the same key, so the
receiver applies the effect at most once, and at least once as soon as one execution
reaches it: item 3. Unsafe: by (c) no execution follows a recorded intent, so the
effect occurs at most once: item 4. $\square$

> **Intuition.** Two gaps remain: a model answer that arrives just before a crash is paid for twice, and the whole scheme assumes only one clerk works from the logbook at a time.

**Remark 3.6 (Two known gaps).** A model call that completes but whose output is not
yet journaled when the crash occurs is requested again after recovery: its cost is paid
twice and recorded once, so spend is undercounted by at most one call per crash.
And the guarantees assume a single process resumes a run at a time; two concurrent
resumers would both act on the same intents. Excluding that is the job of a **lease**
[GC89], and rejecting a stale holder that resumes late is the job of a **fencing token**,
a monotonically increasing number checked on every write (Chubby's sequencers [Bur06]
play this role). Both return in the ledger chapter.

## Common misconceptions

- *"After a crash, an unsafe action is not repeated because the runtime does not know
  what it was."* It knows exactly what it was: the arguments are journaled. It does not
  know **whether** it happened (Lemma 3.3).
- *"Retrying is always safe if the operation is the same."* Only for read, idempotent
  or keyed effects (Theorem 3.5); otherwise retrying trades loss for duplication
  (Theorem 3.4).
- *"On recovery, simply ask the model again."* That recomputes a decision whose
  consequences are already recorded (Proposition 3.2).

## In this project (Stage 3, [`03-resumable-agent/`](../../03-resumable-agent/))

- `journal.py`: Definition 3.1; append with `fsync`, a torn final line discarded,
  damage elsewhere reported as corruption.
- `durable_agent.py`: `apply()` is the transition function of Definition 3.2, used
  identically live and on replay; `_settle()` implements Definition 3.3; `_recover()`
  implements rule (c) of Theorem 3.5.
- `effects.py`: `send_message` (keyed) and `append_note` (unsafe).
- `crash_demo.py`: the process is killed with `os._exit` immediately after an effect
  and before its result is journaled, the window of Lemma 3.3. A restart without the
  journal performed each effect twice; the durable runtime performed each exactly once:
  the message by key deduplication, the note by refusing to retry.
- `test_durable.py`: every crash point, including a test that asserts the gap of
  Remark 3.6 rather than hiding it.

## Refresher

State is a fold over an append-only journal, so recovery is replay. Decisions of the
model are logged, never recomputed. Every action is announced in the journal before it
happens; after a crash, an announced action without a result may or may not have
happened, and no reading of the journal can tell which. Read and idempotent actions
are re-run, keyed actions are re-run under the same key, and unsafe actions are not
re-run and are reported as outcome unknown: exactly-once for those needs the
receiver's cooperation.

*References:* [CGR11] [Moh92] [GR93] [BN84] [Sch90] [GC89] [Bur06]. Full list: [references.md](references.md).
