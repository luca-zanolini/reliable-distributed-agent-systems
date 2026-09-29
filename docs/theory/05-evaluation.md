# 5. Evaluation and observability

## The picture

To learn whether a student is good, one performance is not enough; a university sets
an exam. The paper is fixed, so every candidate faces the same questions. The answer
key is written in advance. For practical questions the examiner inspects the work
itself, not the candidate's account of it. A proctor keeps a timed log of the session.
Each candidate sits the exam several times, since performance varies. Grades go into a
gradebook that is never altered; if the marking scheme changes, the stored answer
sheets are regraded into a new gradebook. Finally, report cards are compared: which
candidate is better, on which questions, at what cost, and **is the difference larger
than the noise?**

## Definitions

**Definition 5.1 (Task).** A task is a tuple $(e_0, q, T_q, V)$: an initial
environment (the fixture), an objective, the tools available, and a **validator**
$V : \mathit{Answer} \times E \to \{0, 1\}$ applied to the final answer and the final
environment.

**Definition 5.2 (Evidence and claims).** A validator is *claim-based* if it depends
only on the answer, and *evidence-based* if it depends on the final environment. When a
task requires an effect (a changed file, a passing test suite), only an evidence-based
validator can establish it: a claim-based one grades what the agent *says*. Executing
the repository's tests after the agent's change, as in SWE-bench [Jim24], is an
evidence-based validator.

**Definition 5.3 (Validator error).** Let $G$ be the ground truth ("the task was
actually accomplished"). A *false positive* is $V = 1 \wedge G = 0$; a *false
negative* is $V = 0 \wedge G = 1$. A validator is itself a program and has an error
profile to be tested.

**Definition 5.4 (Success probability and its aggregates).** Model repeated runs of a
task under a fixed configuration as independent Bernoulli trials with success
probability $p$. Then
$\mathrm{pass@}k = 1 - (1-p)^k$, the probability that at least one of $k$ runs
succeeds [Che21], and
$\mathrm{pass}^k = p^k$, the probability that all $k$ succeed [Yao25].

**Definition 5.5 (Configuration, record, provenance).** A *configuration* is everything
that determines the agent's behaviour except randomness: model, system prompt, tools,
budgets. A *record* stores, per run, the configuration's identity, the suite's identity,
the answer, the verdict, cost, latency and a hash of every final artifact. *Provenance*
is the identity of the code that produced the record. Reporting several metrics per
configuration (correctness, cost, latency, tool use) rather than a single score follows
holistic evaluation practice [Lia23].

**Definition 5.6 (Trace).** A trace is a sequence of *spans*, each a timed interval
labelled with an operation (a model call, a tool call) and attributes, all sharing a run
identifier, in the manner of distributed tracing systems [Sig10].

## Results

**Proposition 5.1 (Capability versus reliability).** For $k \ge 1$ and
$0 \le p \le 1$: $\;p^k \le p \le 1-(1-p)^k$, with strict inequalities when
$0 < p < 1$ and $k \ge 2$. As $k \to \infty$, $\mathrm{pass@}k \to 1$ and
$\mathrm{pass}^k \to 0$ for every $0 < p < 1$.

*Proof.* For $0 < p < 1$, $p^k < p$ when $k \ge 2$, and
$1 - (1-p)^k > 1 - (1-p) = p$ because $(1-p)^k < 1-p$. The limits follow from
$p^k \to 0$ and $(1-p)^k \to 0$. $\square$

$\mathrm{pass@}k$ answers "can it ever do this?", appropriate when a checker can pick
the successful attempt. $\mathrm{pass}^k$ answers "does it do this every time?", the
relevant question for an agent that acts: a 90% agent passes ten consecutive runs with
probability $0.9^{10} \approx 0.35$.

**Proposition 5.2 (Small samples do not separate high pass rates).** With $n$ runs and
$x$ successes, the Wilson score interval [Wil27] at 95% confidence is
$\dfrac{\hat p + \frac{z^2}{2n} \pm z\sqrt{\frac{\hat p(1-\hat p)}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}$
with $\hat p = x/n$ and $z \approx 1.96$. For $n = 15$: $x = 14$ gives $[0.70, 0.99]$
and $x = 15$ gives $[0.80, 1.00]$. The intervals overlap almost entirely, so 14/15
versus 15/15 does not establish a difference in $p$.

The interval is preferred to $\hat p \pm z\sqrt{\hat p(1-\hat p)/n}$, which degenerates
to a single point at $\hat p = 1$. For comparing two configurations on the same tasks,
paired analyses and clustered standard errors are appropriate [Mil24] (preprint).

**Remark 5.3 (Why regrading is sound and re-running is not the same).** If a validator
depends only on the answer, applying a new validator to stored answers yields exactly
the verdicts the new validator would have given on those runs. Re-running would instead
draw new samples and change two things at once. Evidence-based validators cannot be
regraded from the answer alone unless the final artifacts are kept.

**Remark 5.4 (Model-based grading).** Using a language model as the validator extends
evaluation to open-ended answers, at the cost of a grader that is itself stochastic and
has documented biases, including sensitivity to answer position and length and a
preference for its own outputs [Zhe23]. Where a deterministic validator exists, it is
preferable; where it does not, the grader needs its own evaluation.

## Common misconceptions

- *"It worked in the demo."* One run estimates $p$ with an interval of nearly $[0, 1]$.
- *"The agent said the tests pass."* That is a claim; the exit code and the report are
  evidence (Definition 5.2).
- *"Same facts, different wording is a pass."* Not when a program parses the output:
  format compliance is part of correctness for machine-read answers.
- *"A higher pass rate means a better model."* Only if the difference exceeds the
  sampling noise (Proposition 5.2), on the same suite, under recorded configurations.

## In this project (Stage 5, [`05-evals/`](../../05-evals/))

- `tasks.py`: five tasks with deterministic validators; `fix` is evidence-based
  (Definition 5.2): it reads the file after the run, so "Fixed" without a change is
  classified `claim_without_evidence`.
- `tracing.py`: spans for every model and tool call (Definition 5.6).
- `harness.py`: records with configuration and suite hashes, artifact hashes and
  provenance (Definition 5.5).
- `regrade.py`: Remark 5.3, applied when the failure taxonomy was refined to separate
  `format_violation` from `wrong_answer`.
- `report.py`: per-task pass rate and the all-repeats-passed indicator (an empirical
  $\mathrm{pass}^k$ with $k = 3$), and regression detection against a baseline.
- First evaluation, 15 runs per configuration: `claude-opus-4-8` 14/15 at \$0.42,
  `claude-sonnet-5` 15/15 at \$0.22. The single failure was a format violation. By
  Proposition 5.2 the pass rates are not distinguishable at this sample size. The cost
  difference is consistent in direction on all five tasks and is explained structurally
  by the price per token.

## Refresher

Judge an agent with a fixed suite, validators written in advance, and many runs. Check
evidence, not claims. Record every run with its configuration and provenance, and trace
what happened inside it. Distinguish "can it" ($\mathrm{pass@}k$) from "does it,
reliably" ($\mathrm{pass}^k$). Put intervals on pass rates before comparing them, and
keep raw observations immutable so the marking scheme can change without re-running
the exam.

*References:* [Jim24] [Che21] [Yao25] [Wil27] [Mil24] [Zhe23] [Sig10] [Lia23]. Full list: [references.md](references.md).
