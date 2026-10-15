# 10. Byzantine participants

## The picture

Until now everyone in the office could faint, but nobody lied. Now some of them may. The
clerk who writes the letters may slip a note to the reviewers inside one ("this was
pre-approved, sign it"); a reviewer may sign anything; the courier may post a letter of his
own; even a manager at the counter may stamp "approved" on a page nobody reviewed, or quietly
throw the reviewers' slips in the bin.

Checking everyone's honesty is hopeless, so the office changes where the decision is taken.
The post room, the last door before the street, no longer takes anyone's word. Every
letter must arrive with three things it can check by itself: the original request, signed by
the department that is allowed to ask; the letter; and enough reviewers' signatures, each on
this very letter for this very request, from reviewers whose specimen signatures hang on the
post room's own wall. The wall was put up by the director, not by anyone who brings letters.
A courier can still refuse to carry a letter, and a manager can still bin the slips; but no
letter leaves that the right people did not ask for and approve. And when a manager bins the
slips, the reviewers still hold the manager's signed receipts, and can prove it.

One more lesson hides in the reviewers. Three reviewers who were all trained on the same
manual, and who all read the clerk's note, are not three opinions; they are one opinion
written three times. What makes a group of signatures convincing is not how many there are,
but whether they could all have been fooled together.

## Definitions

> **Intuition.** A Byzantine participant is one that may do anything at all, including the most damaging thing at the worst moment.

**Definition 10.1 (Byzantine participant; fault model).** A participant is *Byzantine* if it
may deviate arbitrarily from its protocol, including by sending different messages to
different parties and by colluding with other Byzantine participants [LSP82]. A *fault model*
states which participants may be Byzantine together. In this stage the workers, the checkers,
the sender, the front-ends and the store's members may all be Byzantine; the issuer, the
mail service and the operator who installs it are trusted.

An LLM that follows an injected instruction, hallucinates, or reports a success it did not
achieve deviates from its protocol, and from the outside is indistinguishable from a
Byzantine participant. The converse does not hold: modelling a model as Byzantine is
conservative, not descriptive, and what matters is which failures can be *correlated*
(Definition 10.5).

> **Intuition.** A shared secret proves who sent a message only to someone who could have sent it too.

**Definition 10.2 (Message authentication; transferability).** A *message authentication
code* (MAC) is computed and checked with the same secret key [BCK96]. A *digital signature*
is computed with a private key and checked with the corresponding public key [JL17]. An
authentication scheme is *transferable* if a third party can check an authenticated message
without being able to produce one.

> **Intuition.** A certificate is evidence that travels: it convinces whoever checks it, wherever it is checked, without trusting whoever carried it.

**Definition 10.3 (Evidence and certificate).** For a task $t$ and a text $m$, the *evidence*
is a triple: the task document signed by the issuer; the text $m$; and a set $C$ of verdicts,
each a document $(H(t), H(m), \textit{approve})$ signed by one checker, where $H$ is SHA-256.
$C$ is a *certificate* for $(t, m)$ under a policy $\mathcal{P}$ if the set of checkers whose
approving verdicts verify (against the pinned keys, for $H(t)$ and $H(m)$) satisfies
$\mathcal{P}$.

> **Intuition.** The gatekeeper's trust is what it was installed with, not what it is told.

**Definition 10.4 (Pinned trust).** The mail service holds a *pinned* configuration, installed
by the operator out of band: the issuer's public key, the checkers' public keys, and the
approval policy. It accepts no change to it over the wire.

> **Intuition.** Say who can fail together, not how many can fail.

**Definition 10.5 (Fail-prone system; witness policy).** A *fail-prone system* $\mathcal{F}$ over
the checkers is a collection of sets, each a set of checkers that may all be faulty together
in one execution, with at most one such set faulty [MR98]. The *witness policy* for $\mathcal{F}$
accepts a set $A$ of approvers if and only if $A \neq \emptyset$ and $A \not\subseteq F$ for
every $F \in \mathcal{F}$. The *threshold policy* for $f$ accepts $A$ if and only if
$|A| \ge f + 1$.

Honest checkers are in *witness mode* when they cannot disagree: on the same text and task,
every honest checker reaches the same verdict, the correct one.

## Results

> **Intuition.** Whoever can check a seal made with a shared secret can make one.

**Proposition 10.1 (MACs are not transferable).** If a party can verify a participant's MAC,
it can produce a valid MAC on any message in that participant's name. In particular, the
Stage 9 front-ends, which verified every participant's HMAC, could forge any participant's
request, and a stored state change carried no evidence of the request that justified it.

*Proof.* Verifying a MAC means recomputing it with the key; the same computation on another
message produces a valid MAC for it. $\square$

> **Intuition.** Some approver is honest exactly when the approvers do not all sit inside one failure domain.

**Lemma 10.2 (Witness quorums).** Let $\mathcal{F}$ be a fail-prone system. A set of approvers
$A$ contains an honest checker in every execution allowed by $\mathcal{F}$ if and only if $A$
is accepted by the witness policy for $\mathcal{F}$. The threshold policy for $f$ is the witness
policy for the fail-prone system of all sets of $f$ checkers.

*Proof.* If $A \subseteq F$ for some $F \in \mathcal{F}$, the execution in which exactly $F$ is
faulty leaves no honest member in $A$. Conversely, if $A \not\subseteq F$ for every $F$, then in
any allowed execution the faulty set $F$ misses some member of $A$, which is honest. For the
threshold: $A$ is contained in some $f$-set if and only if $|A| \le f$. $\square$

> **Intuition.** Nothing goes out that the right people did not ask for and approve, whoever else lies.

**Theorem 10.3 (Safety at the edge).** Assume signatures are unforgeable and SHA-256 is
collision resistant; the pinned configuration is correct; the faulty checkers form a subset of
some $F \in \mathcal{F}$, where $\mathcal{F}$ is the fail-prone system of the pinned witness
policy; and honest checkers are in witness mode. Then every text $m$ the mail service delivers
for a task $t$ was requested by the issuer as $t$ (same id, address and subject), and was judged
correct for $t$ by at least one honest checker. This holds whatever the workers, the sender, the
front-ends and the store's members do.

*Proof.* The mail service delivers $m$ for $t$ only if the issuer's signature on $t$ verifies
under the pinned key, so the issuer signed exactly $t$; and only if the verifying approvals for
$(H(t), H(m))$ come from a set $A$ accepted by the policy. By Lemma 10.2, $A$ contains an honest
checker $c$. A verdict of $c$ verifies only if $c$ signed it, so $c$ signed "approve" for
$(H(t), H(m))$; by collision resistance, that is the hash of the text $c$ judged and of the task it
judged it for. In witness mode $c$'s approval means $m$ is correct for $t$. No other party takes
part in the check: whatever they did, they could not produce the issuer's or $c$'s signature. The
address used is the one in the signed $t$. $\square$

> **Intuition.** Three copies of one weakness approve together, so counting them proves nothing.

**Proposition 10.4 (Counting is unsound under correlated failures).** If some set of $f+1$ or
more checkers can fail together, the threshold policy for $f$ can accept a certificate with no
honest approver.

*Proof.* Let $G$, $|G| \ge f+1$, fail together, and let every member approve $m$. Then $|G| \ge f+1$
is accepted, and $G$ contains no honest checker. $\square$

This is experiment 4: three checkers built on the same (simulated) model obey the same injected
line, and the threshold policy delivers. Three copies of one model are one failure domain, not
three [KL86]; correlated errors across LLMs are measured, not hypothetical [Kim25]. Writing the
domain down, $\mathcal{F} = \{\{l_1, l_2, l_3\}, \{d_1\}\}$ with $d_1$ a deterministic checker, makes
the witness policy demand $d_1$ and at least one of the $l_i$ (experiment 5).

> **Intuition.** Once the gatekeeper checks the evidence itself, a lying office can only refuse to work.

**Proposition 10.5 (A lying ledger loses only liveness).** Under the assumptions of Theorem 10.3,
and with the mail service's own record of delivered task ids, Byzantine front-ends and store
members can cause no delivery that violates Theorem 10.3 and no second delivery for a task. They
can prevent or delay any delivery.

*Proof.* Safety: Theorem 10.3 holds whatever they do. Duplicates: the mail service delivers at
most once per task id from its own durable record (Theorem 8.4), which no other party writes.
Liveness: they can drop requests, never approve, or never hand the approved email to the sender. $\square$

> **Intuition.** A signed "ok" is a promise you can be held to.

**Proposition 10.6 (Dropped votes are provable).** If checkers hold approving verdicts that form a
certificate for $(t, m)$, each with a receipt signed by front-end $f$ acknowledging it, and the
ledger never approves $(t, m)$, then the coordination plane misbehaved, and the receipts prove that
$f$ acknowledged the votes. If the store is honest, $f$ is faulty.

*Proof.* An honest front-end answers a vote only after the vote is committed (Lemma 9.1), and an
honest ledger approves as soon as committed votes satisfy the policy (`ledger.voted`). So either $f$
answered without committing, or the store lost a committed write. The receipts are signed by $f$,
so $f$ cannot deny the answers. If the store is honest, the first case holds. $\square$

The proposition is weaker than it looks: the store signs nothing, so a lie by etcd itself would be
pinned on the front-end that relayed it. Accountability needs every party in the path to sign
what it accepts [HKD07]; the study of when a *liveness* failure can be attributed at all is
[LNRZ25].

> **Intuition.** With $3f+1$ replicas agreeing on one log, believe an answer only when $f+1$ of them give it.

**Proposition 10.7 (Replies from a BFT-replicated rule).** Let $n = 3f+1$ replicas apply a
deterministic rule to a log ordered by Byzantine fault-tolerant consensus [CL02, BKM18], with at
most $f$ replicas Byzantine. An answer given by $f+1$ distinct replicas is the answer of the
honest replicas; an answer given by fewer may be false. Progress requires $2f+1$ replicas to
take part in consensus.

*Proof.* Honest replicas apply the same rule to the same log and give the same answer; $f+1$ answers
include one honest one. A single Byzantine replica can give any answer (experiment 9c). Consensus
for $n = 3f+1$ needs quorums of $2f+1$ to intersect in an honest replica; with fewer than $2f+1$
participating, no block is committed (experiment 9d). $\square$

> **Intuition.** Safety does not have to live where consensus is.

**Remark 10.8 (Where the BFT ledger helps).** Theorem 10.3 does not depend on the ledger. Replicating
the ledger with BFT consensus (Proposition 10.7) adds what the edge cannot check: progress, and the
ledger's internal invariants (one holder per task) despite up to $f$ lying replicas. It does not
remove censorship: a Byzantine leader can delay requests until a view change, and consensus orders
what it is given. The design kept the crash-fault ledger and moved safety to the edge, the
end-to-end argument [SRC84] applied to trust.

> **Intuition.** When honest reviewers may disagree, one honest signature is not enough.

**Remark 10.9 (Agreement mode).** If honest checkers can disagree (a judgement call, as an LLM's
verdict usually is), one honest approval does not establish that $m$ is correct: another honest
checker may have rejected it. The policy then needs a stronger condition (an honest majority
behind the decision, quorums that intersect in honest checkers, or escalation of disagreement to a
human). With asymmetric trust [ACTZ24], each principal may hold its own fail-prone system; what
approval rules guarantee to whom, under correlated failures, is open.

## Common misconceptions

- *"Authenticated messages cannot be forged."* Only by parties without the key. With a MAC, every
  verifier holds the key (Proposition 10.1).
- *"Three approvals out of three checkers is strong evidence."* Only if the three cannot fail
  together (Proposition 10.4). The question is never "how many" but "could they all be wrong at once".
- *"If the ledger is replicated, it can be trusted."* Raft tolerates crashes, not lies; and even a
  BFT ledger can censor (Remark 10.8).
- *"The sender is trusted because it is the only one that can reach the mail service."* Network
  position is a perimeter assumption. Here the sender is a courier: the evidence it carries is
  checked, and its word counts for nothing.
- *"The mail service can receive the checkers' keys from the ledger."* Then a lying front-end adds
  its own key and signs its own approvals (experiment 6b). Trust is pinned, not received.
- *"A hallucinating agent is a Byzantine replica."* An agent that deviates is *treated as* Byzantine;
  the agents here are clients of the coordination plane, never its replicas.
- *"The checkers protect against forged tasks."* They judge the draft against the task they are shown.
  A lying ledger can show them an invented task; only the issuer's signature stops it (experiment 7).

## In this project (Stage 10, [`10-byzantine-agents/`](../../10-byzantine-agents/))

- `sig.py`: Definition 10.2, Ed25519 per RFC 8032, with the RFC's test vectors.
- `evidence.py`: Definitions 10.3–10.5: task and verdict documents, the threshold and witness policies,
  and `check_delivery`, the whole of Theorem 10.3's check.
- `ledger.py`, `frontend.py`: checking as signed votes bound to a draft's hash; approval when the
  votes satisfy the policy; request signatures instead of HMACs; receipts; and the three lies of a
  Byzantine front-end (forge, invent, drop votes).
- `mail_service.py`: the pinned gatekeeper; `--trust-supplied-keys` is the counterfactual.
- `audit.py`: Proposition 10.6.
- `bft.py`, `bft_replica.py`: Proposition 10.7 on CometBFT.
- `experiments.py`: Theorem 10.3 under each lie (1–3, 6, 7), Proposition 10.4 (4) and the fail-prone
  remedy (5), Proposition 10.5 (8), and the counterfactual on pinned trust (6b).

## Refresher

Make every claim carry its own evidence, and let the party with the most to lose check it: the
issuer signs what may be asked, checkers sign what they judged (this text, for this task), and the
mail service, installed with the keys it trusts and the policy it applies, delivers nothing the
evidence does not cover. Then a lying sender, front-end or store can only refuse to work, and their
refusals can be proved from signed receipts. A certificate means "some honest checker approved"
only if its signers could not all have failed together: count failure domains, not signatures.
A BFT ledger adds progress and internal consistency against lying replicas; it is not what keeps
the customer safe.

*References:* [LSP82] [BCK96] [JL17] [MR98] [KL86] [Kim25] [HKD07] [LNRZ25] [CL02] [BKM18] [SRC84]
[ACTZ24]. Full list: [references.md](references.md).
