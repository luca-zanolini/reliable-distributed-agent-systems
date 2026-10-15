# 11. Verifiable work

## The picture

A clerk hands in a corrected page for the office binder and says: "Done. I checked it myself,
everything adds up." The office has learned not to care what clerks say about their own work.
The archive, which keeps the binder, accepts a new page only with three things it can check
itself: the request, signed by the department that may ask for it, with the department's own
checklist attached; a report from an inspector who took this exact page, ran the department's
checklist on it (not the clerk's, and not a copy the clerk could have edited), and signed what
happened; and, for pages in the sections the department marked as sensitive, the signature of a
senior reviewer who read the change. The archive also refuses a page that was written against an
older version of the binder: the binder may have changed since, and the inspector checked a world
that no longer exists.

Two things the archive cannot do. When a checklist item gives different answers each time it is
run, no number of inspectors makes it a proof; the archive sends the page to a person. And a
checklist only checks what it asks: a page written to satisfy those exact questions, and nothing
else, passes. That is what the senior reviewer is for, and why the department decides where one is
needed.

## Definitions

> **Intuition.** Name a version by its contents, and the name proves what is inside.

**Definition 11.1 (Version, head).** A *version* is a set of files; its name is the hash of their
canonical encoding. A *repository* stores versions and one pointer, the *head*: the current version.
A *commit* moves the head to a new version.

> **Intuition.** Evidence of work is an account of something someone else did again, not the worker's account of what it did.

**Definition 11.2 (Recipe; attestation).** A worker's *recipe* names the base version it started
from, the result version, the tests, the command and the environment. An *attestation* is a
document signed by a verifier: it ran the named tests with the named command on the result, in the
named environment, and observed an outcome (pass, fail, or flaky if its own repeated runs
disagreed), with the hash of the output. An attestation reports an execution; a Stage 10 verdict
reports an opinion. Signed records of how an artifact was produced and checked are the subject of
software supply-chain security [TA19]; making builds reproducible is what turns re-execution into a
check anyone can repeat [LZ22].

> **Intuition.** A test is evidence only if it gives the same answer every time.

**Definition 11.3 (Deterministic test).** A test is *deterministic* on a version and an environment
if every run yields the same outcome. Honest verifiers running deterministic tests on the same
version and environment are in witness mode (Definition 10.5): they cannot disagree.

> **Intuition.** The department decides what is checked and where a person must look, in advance, under its signature.

**Definition 11.4 (Pinned tests; rules; required review).** The issuer's signed task names its
acceptance tests by content hash, and gives a *rule* per path prefix: *none* (tests suffice), *any*
(plus one approving review), *human* (plus one approving review by a reviewer pinned as human), or
*forbidden* (the change may not touch the path). The rule of a path is that of its longest matching
prefix, and *human* if none matches. The *required review* of a change is *forbidden* if any changed
path is, and otherwise the most demanding rule among the changed paths.

> **Intuition.** The gate is the last door before the effect, and it computes for itself everything it can.

**Definition 11.5 (Commit gate).** The repository moves its head from $h$ to $r$ for a task $t$ only
if: the issuer signed $t$; the paths changed between the claimed base $b$ and $r$, computed by the
repository itself, require no *forbidden* change; the valid passing attestations for $(t, b, r)$ and
$t$'s pinned tests satisfy the verifier policy; the required review is present, from a pinned
reviewer of the right kind, approving $(t, b, r)$; and $b = h$ (a compare-and-swap).

## Results

> **Intuition.** A worker's report of its own run is compatible with any code at all.

**Proposition 11.1 (Self-reports carry no information).** For any version, a worker can produce the
same report ("all tests pass", any output) of its own run. A report from inside the worker's failure
domain therefore carries no information about the version.

*Proof.* The report is a string chosen by the worker; nothing ties it to an execution. $\square$

This is experiment 1: the worker's own signed "pass" is refused because its key is not a pinned
verifier's, and experiment 3: its claimed pass meets the verifiers' failing runs.

> **Intuition.** If the worker picks the questions, passing the exam says nothing.

**Proposition 11.2 (Who chooses the tests).** If the tests a verifier runs are taken from the
worker's version, a certificate of passing tests carries no information about the task: the worker
can make any version pass. If the tests are named by hash in the issuer's signed task, fetched by
that hash, and the *forbidden* rule protects their paths, then neither what is run nor the tests
future work will be held to can be changed by the worker.

*Proof.* In the first case the worker can replace every test by one that passes. In the second, a
verifier runs exactly the tests whose hashes the issuer signed (the version's copies are overwritten),
and the gate refuses any version that changes a protected path, so the stored tests stay the issuer's. $\square$

Experiment 2: a worker fixed the bug and deleted an acceptance test in its own copy. The verifiers ran
the issuer's tests and signed "pass", correctly; the gate refused the change because it touched
`tests/`.

> **Intuition.** Nothing becomes the current code that the issuer's tests did not pass, that a required reviewer did not approve, or that was built on a different code.

**Theorem 11.3 (Safety of commits).** Assume signatures are unforgeable and SHA-256 is collision
resistant; the repository's pinned trust is correct; faulty verifiers lie within one set of the
policy's fail-prone system; and the pinned tests are deterministic. Then every version $r$ the
repository makes current, for a task $t$, on top of version $b$:
(i) passes $t$'s pinned tests in an attested environment;
(ii) changes no path the issuer marked *forbidden*;
(iii) if $t$'s rules require a review for the paths it changes, was approved by a reviewer of the
required kind; and
(iv) was built on $b$, the code current at the moment of the commit.
This holds whatever the workers, the front-ends, the store's members and the committer do.

*Proof.* (i) The passing attestations accepted by the gate are signed by pinned verifiers about
$(t, b, r)$ and $t$'s pinned tests, and by Lemma 10.2 one signer is honest; that verifier ran exactly
those tests on exactly $r$ (collision resistance) and observed a pass, which, the tests being
deterministic, is the outcome of every run. (ii) The gate computes the changed paths from the two
stored versions itself and refuses *forbidden* ones. (iii) The gate requires a valid approval by a
pinned reviewer whose kind qualifies for the level it computed. (iv) The gate's compare-and-swap.
None of these checks reads anything a worker, the ledger or the committer asserts. $\square$

> **Intuition.** Without "only on top of the current code", two correct changes can make a wrong code.

**Proposition 11.4 (The lost update at the commit point).** If the gate accepted certified versions
built on an older head, a commit could silently undo an earlier one, even though both versions were
correctly certified.

*Proof.* Let $r_1$ and $r_2$ be built on $h$ and certified; commit $r_1$, then $r_2$. The head is $r_2$,
which does not contain $r_1$'s change. $\square$

This is Stage 6's lost update and Stage 9's blind write (Proposition 9.3), at the repository
(counterfactual 5b). With the check, the second change is refused, redone on top of $r_1$, and
re-verified, because its attestations named a base that is no longer current (experiment 5). It is
also why the verifiers need no agreement among themselves: one version at a time becomes current
because the gate is one serialization point, so certificates need $f+1$ witnesses, and $2f+1$
verifiers for liveness, not $3f+1$ (Remark 11.7).

> **Intuition.** When a test answers at random, more inspectors measure the randomness, not the code.

**Proposition 11.5 (Nondeterminism is detectable, not resolvable).** If a pinned test is
nondeterministic, honest verifiers may sign contradictory attestations about the same version,
tests and environment, and two such attestations prove the nondeterminism. No size of quorum makes
the outcome evidence about the version.

*Proof.* Contradictory outcomes of honest verifiers on identical inputs are, by definition, the
failure of Definition 11.3. If the outcome is a random variable independent of the version, any
number of samples estimates its distribution, which is the same for a correct and an incorrect
version. $\square$

The ledger stops such a task at NEEDS_HUMAN, with the reason (experiment 6). The cure is outside the
protocol: quarantine or repair the test, and let a person decide meanwhile [Luo14].

> **Intuition.** Tests ask only the questions they ask.

**Remark 11.6 (Overfitting to the tests).** A version can pass every pinned test and still be wrong,
by special-casing the tested inputs; this is a known failure of automated program repair [Smi15].
Experiment 7: a fix that rejects only the token value the test uses passed the pinned tests (three
honest attestations) and failed the held-out ones. Two defences, both outside Theorem 11.3: held-out
tests the worker never sees, and review where the issuer marks it necessary. A review is an opinion
(Stage 10), so its kind matters: a bot reviewer obeyed a comment telling reviewers to approve
(counterfactual 7c), while a rule demanding a human made the bot's approval irrelevant (7d). A bot
built on the same model as the worker would also share its blind spots [Kim25].

> **Intuition.** Witnesses need not agree with each other; voters do.

**Remark 11.7 (2f+1 verifiers, not 3f+1).** $3f+1$ is the price of quorum intersection: two quorums of
$n-f$ must share an honest member so that conflicting decisions cannot both be made. Verifiers decide
nothing between themselves; each certificate needs only one honest witness ($f+1$ signers, safety
for any $n$), and liveness needs $f+1$ honest verifiers despite $f$ silent ones ($n \ge 2f+1$).
Uniqueness comes from the gate. A ledger that must itself agree despite lying replicas pays $3f+1$
(Proposition 10.7).

## Common misconceptions

- *"The agent ran the tests and they passed."* A report of one's own run is not evidence of anything
  (Proposition 11.1); only a run by someone outside the worker's failure domain is.
- *"A test suite in the repository is the specification."* Not if the change being judged may edit it
  (Proposition 11.2). The tests belong to the task, pinned by its issuer.
- *"Certified changes can be committed in any order."* Each certificate is about a change on a given
  base; committing it on another base commits code nobody tested (Proposition 11.4).
- *"A flaky test passes most of the time, so a majority of runs decides."* A majority of random answers
  is still random (Proposition 11.5).
- *"Passing tests means the fix is right."* It means the fix answers those questions right (Remark 11.6).
- *"A reviewer is a reviewer."* A human and an LLM reviewer are different failure domains, and only
  one of them obeys instructions written in the code under review.

## In this project (Stage 11, [`11-verifiable-work/`](../../11-verifiable-work/))

- `project.py`: the code, its pinned tests, every change a worker can make, and the held-out tests.
- `evidence.py`: Definitions 11.2–11.5: attestations, reviews, rules, and `check_commit`.
- `repo_service.py`: the gate (Definition 11.5): versions by content hash, changed paths computed
  locally, compare-and-swap on the head, retried commits recognised.
- `ledger.py`, `frontend.py`: work as versions; approval from attestations and required reviews;
  NEEDS_HUMAN on proven nondeterminism; a stale base sends the work back.
- `agent.py`: workers (honest and four ways of cheating), verifiers that run the pinned tests in a
  fresh directory, reviewers (human and bot stand-ins); `committer.py`.
- `experiments.py`: Propositions 11.1 (1, 3), 11.2 (2), Theorem 11.3 under a lying verifier (4) and a
  lying front-end (8b), 11.4 (5, 5b), 11.5 (6), Remark 11.6 (7, 7b, 7c, 7d), and the cheap path (8).

## Refresher

Never commit on what a worker says about its own work. Let the issuer pin the acceptance tests and
mark where a person must look; let verifiers run exactly those tests on exactly that version and sign
what happened; let the repository, which owns the current code, compute what changed, check every
signature against keys it was installed with, and accept a change only on top of the code it was
tested on. Then nothing becomes current that did not pass the issuer's tests, change only what it may,
and get the review the issuer required. What remains is what tests cannot say: they can be flaky,
which can be detected but not voted away, and they can be answered without being understood, which
is what held-out tests and reviewers are for.

*References:* [Smi15] [Luo14] [TA19] [LZ22] [Kim25] [SRC84] [KR81]. Full list: [references.md](references.md).
