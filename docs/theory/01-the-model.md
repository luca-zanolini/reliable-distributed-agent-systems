# 1. The model

## The picture

A model is an oracle behind a counter. You slide a sheet of paper under the glass;
it slides back a sheet with an answer written on it. It keeps nothing: the next
person at the counter, or you a second later, meets an oracle that has never seen
you. If you want it to remember your earlier question, you must copy the earlier
question and its answer onto the new sheet yourself. It also never writes the same
answer twice in quite the same words, and when the sheet runs out of space it simply
stops mid-sentence and slides it back.

Everything in this repository is built around three facts in that picture: the
oracle is **stateless**, **stochastic**, and **indifferent to completeness**.

## Definitions

Let $V$ be a finite vocabulary of tokens, $V^*$ the finite sequences over $V$, and
$\Delta(X)$ the probability distributions over a set $X$.

**Definition 1.1 (Language model).** A language model is a map
$\pi : V^* \to \Delta(V \cup \{\mathsf{eos}\})$ assigning to every prefix a
distribution over the next token or end-of-sequence. Modern instances are
Transformer networks [Vas17]; nothing below depends on the architecture.

**Definition 1.2 (Generation).** Given an input $x \in V^*$ and a cap $m$, generation
samples $y_1, y_2, \ldots$ with $y_i \sim \pi(x \cdot y_{<i})$ until $\mathsf{eos}$
is drawn or $i = m$. It returns $(y, s)$ with stop reason
$s = \mathsf{end}$ if $\mathsf{eos}$ was drawn and $s = \mathsf{truncated}$ if the
cap was reached. Sampling procedures (temperature, nucleus sampling [Hol20]) are
transformations of $\pi$ applied before drawing; current production models may not
expose them.

**Definition 1.3 (Model call).** A model call is a randomized procedure
$\mathsf{Call}(x) = (y, s, u)$ where $(y,s)$ is a generation and
$u = (u_{\mathrm{in}}, u_{\mathrm{out}})$ is a *receipt*: the number of input and
output tokens. With per-token prices $\alpha, \beta$, its cost is
$c(u) = \alpha\, u_{\mathrm{in}} + \beta\, u_{\mathrm{out}}$. On current models
$\beta / \alpha = 5$.

**Definition 1.4 (Statelessness).** Calls are stateless if, for any sequence of
calls with inputs $x_1, \ldots, x_n$, the $n$-th output is independent of all earlier
inputs and outputs given $x_n$:
$\Pr[\mathsf{Call}(x_n) \mid x_1, o_1, \ldots, x_{n-1}, o_{n-1}] = \Pr[\mathsf{Call}(x_n)]$.

**Definition 1.5 (Constrained decoding).** Let $L \subseteq V^*$ be a language (for
example, the serializations of a JSON Schema) and $\mathrm{Pref}(L)$ its set of
prefixes. Constrained generation draws each token from $\pi$ restricted to tokens $v$
with $y_{<i} \cdot v \in \mathrm{Pref}(L)$, permits $\mathsf{eos}$ only if
$y \in L$, and renormalizes [Gen23, WL23].

## Results

**Proposition 1.1 (Conversation state is client state).** Let an application build
the input of its $t$-th call as $x_t = \mathrm{Enc}(h_{t-1}, q_t)$ from its own
history $h_{t-1}$ and a new query $q_t$. Under statelessness, the distribution of the
$t$-th reply depends on the past only through $x_t$.

*Proof.* Immediate from Definition 1.4: conditioning on earlier inputs and outputs
does not change the distribution of $\mathsf{Call}(x_t)$. $\square$

**Corollary 1.2 (Amnesia).** If $x_t$ omits earlier exchanges, the reply is
distributed as in a fresh conversation. **Corollary 1.3 (Roles are labels).** Two
histories with the same encoding are indistinguishable to the model: a fabricated
"assistant" turn is treated exactly like a genuine one. Whatever trust a conversation
carries lives in the application that assembled it, not in the transcript.

**Proposition 1.4 (Input size is deterministic).** $u_{\mathrm{in}}$ is a function
of $x$ alone. *Proof.* Tokenization is a deterministic function of the input. $\square$
All variability in size and cost is therefore on the output side.

**Proposition 1.5 (Soundness of constrained decoding).** If constrained generation
returns with $s = \mathsf{end}$, then $y \in L$.

*Proof.* By induction on $i$, every prefix $y_{\le i}$ is in $\mathrm{Pref}(L)$,
since only tokens preserving membership are permitted. $\mathsf{eos}$ is permitted
only when $y \in L$. $\square$

Two consequences matter in practice. First, the proposition says nothing when
$s = \mathsf{truncated}$: a truncated structured output is in general a proper prefix
of a valid one and **not** itself valid, so truncation must be rejected, not parsed.
Second, membership in $L$ is **syntactic**: every element of $L$ with positive
probability may be produced, including false ones. A schema constrains the *shape*
of an answer, never its *truth*; truth needs a verifier (Chapter 5).

**Remark 1.6 (Completeness is not transport success).** A call can succeed at every
layer of transport (the protocol reports success) and still return
$s = \mathsf{truncated}$. Completeness is a property of the stop reason, which the
caller must check.

## Common misconceptions

- *"The model remembers the conversation."* The application re-sends it; the model
  sees one input at a time (Proposition 1.1).
- *"Fixing the output format fixes correctness."* It fixes the shape (Proposition
  1.5), and in our measurements it also removed variation, but it cannot make a false
  value true.
- *"A successful response is a complete response."* Only the stop reason says so
  (Remark 1.6).

## In this project (Stage 1, [`01-llm-runtime/`](../../01-llm-runtime/))

- `amnesia.py` demonstrates Proposition 1.1 and Corollary 1.3: separate calls share
  nothing, and a replayed history, including fabricated turns, restores "memory".
- `measure.py` measured Proposition 1.4 and the output-side variability: over 30 runs
  per arm, input tokens were constant (154 free-form, 456 structured), free-form output
  ranged 265–405 tokens, and structured output was 56 tokens with a single distinct
  answer in 30.
- One free-form run stopped at exactly the 300-token cap with no error (Remark 1.6);
  the adapter in `llm.py` now raises on truncation.
- `llm.py` makes the receipt (Definition 1.3) a first-class value, `Completion`, and
  hides the vendor behind an interface, with a scripted `FakeProvider` for tests.

## Refresher

A model call is a stateless, randomized function from text to text with a receipt.
Memory is whatever the caller re-sends; cost is linear in tokens and dominated by
output; structure can be enforced during decoding, but only shape, not truth; and a
response is complete only if its stop reason says so.

*References:* [Vas17] [Hol20] [Gen23] [WL23]. Full list: [references.md](references.md).
