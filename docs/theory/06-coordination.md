# 6. Coordination

## The picture

The office now has a team. A manager pins a list of jobs on a whiteboard; two clerks
each walk up, read the board, pick a job, go to the shared filing cabinet, rework a
document, and come back to tick the job off. A checker tests the documents; an editor
reads them; the manager signs off when every job is ticked.

Nothing here is wrong in any single step. The trouble is **time**. Both clerks can read
"job 1: free" in the same minute and both start it. One clerk can take the document out,
the other takes the same version out, and whoever puts theirs back second erases the
first one's work. The checker can test Tuesday's version while the manager signs
Wednesday's. And if the manager's whiteboard is wiped, everyone starts again, redoing
what was done. Coordination is the discipline of making *time* harmless.

## Definitions

> **Intuition.** Several agents share one objective and one body of state, and a coordinator holds the shared records. The agents are each as in Chapter 2; what is new is that they act on the same things.

**Definition 6.1 (Multi-agent system).** A set of agents (Chapter 2) that share an
objective and mutable state: a task list, held by a **coordinator**, and a repository of
artifacts. The system is justified only if it improves parallelism, specialization,
isolation or independent verification over a single agent.

> **Intuition.** Seeing a concurrent system as one long list of small moves, in some order, lets us ask "is there an order in which this goes wrong?"

**Definition 6.2 (Step, schedule).** Each agent's behaviour is a sequence of **steps**,
each an indivisible action on shared state (a read or a write). An execution is a
**schedule**: an interleaving of the agents' steps. A property holds for the system only
if it holds for **every** schedule.

The next six definitions are the distinctions that naive multi-agent systems blur.

> **Intuition.** Deciding what the pieces of the job are.

**Definition 6.3 (Decomposition).** The division of an objective into tasks, each with a
description and the artifacts it concerns.

> **Intuition.** Deciding who does what, and in what order.

**Definition 6.4 (Scheduling).** The assignment of tasks to agents and the order in which
they are attempted.

> **Intuition.** Knowing, at every moment, who holds a task, so that two people never hold it at once.

**Definition 6.5 (Ownership).** A relation between tasks and agents, maintained by the
coordinator. Ownership is **exclusive** if no task has two owners at any moment.

> **Intuition.** Give every version of the code a name that is computed from its contents, so that "the same version" and "the same contents" mean the same thing.

**Definition 6.6 (Content-addressed version).** A version of the repository is identified
by $\mathrm{id}(v) = H(v)$, a collision-resistant hash of its contents. Every artifact
derived from the code (a patch, a test report, a review) carries the identifier of the
version it was derived from.

> **Intuition.** A check counts only for the version it was run on.

**Definition 6.7 (Verification bound to a version).** A verification record is a pair
$(\mathrm{id}, \mathit{verdict})$: the verdict (tests passed, review approved) applies to
the version with that identifier and to no other.

> **Intuition.** The single decision that says "this version is the result", which must rest on evidence about exactly that version.

**Definition 6.8 (Acceptance).** The coordinator's decision that a version is the result
of the objective. Acceptance is an irreversible commit: after it, the version is what the
system delivers.

## Results

> **Intuition.** If "is it free?" and "I take it" are two separate moves, someone else can make both of their moves in between.

**Proposition 6.1 (Check-then-act admits two owners).** If claiming a task consists of a
read of its status followed by a separate write of its owner, there is a schedule in which
the task has two owners.

*Proof.* Let agents $A$ and $B$ claim task $t$, with steps $r_A, w_A$ and $r_B, w_B$. The
schedule $r_A\, r_B\, w_A\, w_B$ is admissible: both reads return "open", so both agents
proceed, and after $w_A$ and before $w_B$ the owner is $A$, while $B$ has already decided
to own $t$. $\square$

> **Intuition.** If "take it only if it is still free" is a single, indivisible move, then only the first taker succeeds; everyone else is told it is taken.

**Proposition 6.2 (An atomic conditional claim gives exclusive ownership).** If a claim is
a single linearizable [HW90] compare-and-set that changes a task from open to owned by
the caller only if it is open, then ownership is exclusive.

*Proof.* Linearizability orders all claims on $t$ in a sequence consistent with real time.
The first claim in that order finds $t$ open and succeeds; every later one finds it owned
and fails. Hence at no point do two agents own $t$. $\square$

Exclusivity alone is not enough when owners can crash while holding a task: a **lease**
(ownership that expires [GC89]) restores progress, and a **fencing token** rejects a former
owner that acts after its lease has passed [Bur06]. Chapter 3's single-resumer assumption
is the same requirement.

> **Intuition.** If you save your edited copy without checking that nobody saved in the meantime, you may erase their work. Saving only if the cabinet still holds the version you started from makes that impossible.

**Proposition 6.3 (Lost updates and optimistic concurrency).** If writes are applied to the
current head regardless of the version the writer read, a committed change can be discarded
by a later write. If instead each write names its base version and commits only if the head
still equals that base, then every committed version is derived from its immediate
predecessor, and no committed change is overwritten by a write derived from an older version.

*Proof.* First claim: the schedule "$A$ reads $v_0$; $B$ reads $v_0$; $A$ commits
$v_1 = f_A(v_0)$; $B$ commits $f_B(v_0)$" replaces $v_1$ by a version computed without
$A$'s change. Second claim: a write with base $b$ commits only if $\mathit{head} = b$, so the
new version is computed from the current head; a write whose base is older fails and must be
recomputed from the new head. This is validation in optimistic concurrency control [KR81];
the anomaly is the lost update of the concurrency-control literature [BHG87]. $\square$

Merging both writes automatically would avoid the loss but not the problem: a
conflict-free replicated data type guarantees that replicas **converge** [SPBZ11], not that
the converged state is **meaningful**, and two individually correct patches can combine into
an incorrect program.

> **Intuition.** Because names are computed from contents, comparing two names tells you exactly whether two things refer to the same version, and a mismatch is a stale reference.

**Proposition 6.4 (Content addressing makes staleness decidable).** Under collision
resistance, $\mathrm{id}(v) = \mathrm{id}(w)$ if and only if $v = w$ (except with negligible
probability). Hence whether a verification record concerns the version being accepted is
decided by comparing identifiers.

*Proof.* If $v = w$ the hashes are equal. If $v \ne w$ and the hashes are equal, $(v,w)$ is a
collision. $\square$

> **Intuition.** Accept a version only if it was tested, and reviewed, as that very version, and nobody has changed it since. Then what you accept is what you checked.

**Proposition 6.5 (Version-bound acceptance is sound).** Suppose the coordinator accepts $h$
only if (i) a passing test record for $h$ exists, (ii) an approving review record for $h$
exists, and (iii) $h$ is the head, with (i)–(iii) checked and the acceptance recorded in one
atomic step. Then the accepted contents are exactly the contents that were tested and reviewed.

*Proof.* By (i), (ii) and Proposition 6.4, the tested, reviewed and accepted versions have
the same contents. By (iii) and atomicity, no write intervenes between the check and the
acceptance. $\square$

The naive acceptance rule, "all tasks done, some test passed, some review approved",
omits the version in (i)–(ii) and the atomicity in (iii); scenario c shows it accepting a
version that fails its tests.

> **Intuition.** The same visible problem, work done twice, can come from three different causes, and each cause needs a different cure.

**Remark 6.6 (One symptom, three causes).** Duplicated effects arise from a stale read of
the task list (Proposition 6.1), from a retry without deduplication (at-least-once delivery;
Chapter 3, Definition 3.5) and from a coordinator that forgot completed work (Chapter 3,
Proposition 3.1 applied to the coordinator). The cures are, respectively, conditional
ownership, idempotency keys, and a durable coordinator.

> **Intuition.** More agents are not better by default: they add coordination problems, and the gains have to outweigh them.

**Remark 6.7 (When not to use multiple agents).** Role-based designs structure work as an
assembly line of specialized agents [Hon24]; structure is not concurrency control, and every
shared artifact reintroduces the problems above. An empirical taxonomy of failures in
multi-agent language-model systems groups them into specification issues, inter-agent
misalignment and task verification, and reports that performance gains over single agents
are often minimal [Cem25]. A shared, inspectable workspace is the classical *blackboard*
architecture [Nii86]; it makes coordination visible, not correct.

## Common misconceptions

- *"Two agents, two tasks: they cannot interfere."* They share a file, a task list and a
  head; every shared object is a place to interfere (Propositions 6.1, 6.3).
- *"A shared board prevents stale information."* Agents read it at different moments; what
  they act on may already be out of date.
- *"Merging concurrent edits automatically fixes conflicts."* It guarantees convergence, not
  a correct program (Proposition 6.3, remark).
- *"Each worker journaling its steps prevents repeated work."* It prevents a worker from
  repeating its own action after a crash, not another worker from repeating it
  (Remark 6.6).
- *"If every failure is visible in the logs, the system is safe."* Detection is a comparison;
  safety needs the coordinator to refuse what the comparison reveals (Proposition 6.5).
- *"A review is verification."* Only if the reviewer examines evidence about the version it
  approves (Definition 6.7).

## In this project (Stage 6, [`06-multi-agent/`](../../06-multi-agent/))

- `repo.py`: Definition 6.6; writes are deliberately unconditional, so Proposition 6.3's
  first claim can be observed.
- `coordinator.py`: the board, ownership and acceptance without the conditions of Propositions 6.2 and 6.5;
  every action recorded with its version identifier.
- `agents.py`: agents as generators pausing after each step, and a scheduler that realizes
  any schedule of Definition 6.2.
- `scenarios.py`: the six schedules. Double claim (Proposition 6.1), stale review and
  untested acceptance (Proposition 6.5 violated), retry duplication and coordinator loss
  (Remark 6.6), lost update (Proposition 6.3). Five of the six end in an acceptance.

## Refresher

When several agents share state, correctness is a property of every interleaving, not of
any single agent. Check-then-act admits two owners; a single conditional step does not.
Writes that ignore their base lose updates; writes conditional on their base do not. Name
versions by their contents, bind every piece of evidence to a version, and accept a version
only on evidence about that very version while it is still the head. Duplicated work has
several causes, each with its own cure, and more agents are justified only when their gains
exceed these costs.

*References:* [HW90] [GC89] [Bur06] [KR81] [BHG87] [SPBZ11] [Hon24] [Cem25] [Nii86]. Full list: [references.md](references.md).
