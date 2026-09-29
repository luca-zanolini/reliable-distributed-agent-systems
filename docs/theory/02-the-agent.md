# 2. The agent

## The picture

Put the oracle of Chapter 1 behind a desk clerk. The oracle still only writes on
paper; it cannot open a drawer or touch anything. But now, instead of answering, it
may write a *request*: "please fetch me the file `log.txt`". The clerk reads each
request, checks it against the rules of the office (is this a service we offer? is
the form filled in correctly? is that drawer one this oracle may open?), and only
then walks to the drawer. The clerk copies the result, together with every earlier
sheet, onto a fresh sheet and slides it back under the glass. This repeats until the
oracle writes an answer instead of a request, or the clerk decides enough time and
money have been spent.

The clerk and the office are the **runtime**. The oracle plus the clerk, working in
this loop, is the **agent**. The oracle chooses what to ask for; the clerk alone
decides what happens.

## Definitions

**Definition 2.1 (System).** An agent system consists of a model $M$ (Chapter 1),
treated as untrusted; a **runtime** $R$, a deterministic program treated as trusted;
a set of tools $T$; and an environment with state space $E$ (files, services). The
model is outside the runtime: the runtime reaches it only through model calls.

**Definition 2.2 (Tool).** A tool $t$ has a name, an argument schema
$\Sigma_t$ (the admissible arguments), an effect
$f_t : E \times \Sigma_t \to E \times \mathit{Out}$, and a footprint
$\mathit{fp}_t : \Sigma_t \to 2^{\mathit{Res}}$, the set of resources an invocation
may read or modify.

**Definition 2.3 (Request).** A tool request is a triple
$r = (\mathit{id}, \mathit{name}, \mathit{args})$ appearing in model output. It is a
syntactic object: producing it has no effect on $E$.

**Definition 2.4 (Gate).** The runtime's gate maps each request to
$\mathsf{permit}$ or $\mathsf{refuse}(\mathit{reason})$, from the request and the
runtime's own state. A permitted request is executed, $f_t$ is applied, and its
output (bounded in time and size) becomes the result; a refused request is not
executed and the result is the reason. **Either way, exactly one result, carrying the
request's $\mathit{id}$, is returned to the model.**

**Definition 2.5 (Run).** Given an objective $q$, step budget $N$ and cost budget
$B$: let $h_0 = \langle q \rangle$ and $\mathit{spent} = 0$. For
$k = 1, \ldots, N$: if $\mathit{spent} \ge B$, end with $\mathsf{cost\_budget}$;
otherwise call the model on $h_{k-1}$, obtaining text $a_k$ and requests
$r_1, \ldots, r_m$ and adding the call's cost to $\mathit{spent}$. If the call fails,
end with $\mathsf{aborted}$. If $m = 0$, end with $\mathsf{answered}(a_k)$. Otherwise
compute results $\rho_i = \mathrm{Gate}(r_i)$ and set
$h_k = h_{k-1} \cdot (a_k, r_{1..m}) \cdot (\rho_{1..m})$. If the loop completes,
end with $\mathsf{step\_budget}$. This interleaving of model reasoning and tool
actions is the ReAct pattern [Yao23]; tool use by language models is studied in
[Sch23].

**Definition 2.6 (Complete mediation).** A runtime satisfies complete mediation if
every change to $E$ during a run is the effect $f_t$ of a request the gate permitted
[SS75]. A mechanism that mediates every access, cannot be bypassed and is small
enough to verify is a *reference monitor* [And72]; the gate is one.

## Results

**Theorem 2.1 (Confinement against arbitrary model output).** Suppose (i) complete
mediation holds, (ii) the gate permits a request only if
$\mathit{fp}_t(\mathit{args}) \subseteq W$ for a set of resources $W$ (the
workspace), and (iii) each $f_t$ reads and modifies only resources in its footprint.
Then **for every model**, that is, for every function from histories to outputs,
including an adversarial one, every change to $E$ in every run is confined to $W$.

*Proof.* By induction on the steps of a run. The model's outputs enter the system only
as requests (Definition 2.3), which have no effect by themselves. By (i), every change
to $E$ is the effect of a permitted request; by (ii) and (iii), such an effect touches
only $\mathit{fp}_t(\mathit{args}) \subseteq W$. No step depends on which outputs the
model produced. $\square$

The quantifier is the point: the guarantee holds against *any* model behaviour, the
worst case of distributed computing's Byzantine fault model applied to a single
untrusted proposer. It rests entirely on the runtime, which is the trusted computing
base.

**Theorem 2.2 (Termination and bounded spend).** Every run ends after at most $N$
model calls. If every call costs at most $c_{\max}$, total spend is below
$B + c_{\max}$.

*Proof.* The loop performs at most one call per iteration and has $N$ iterations.
Spend is checked before each call, so before the last call $\mathit{spent} < B$,
and after it $\mathit{spent} < B + c_{\max}$. $\square$

Theorem 2.1 is a **safety** property (nothing bad happens) and Theorem 2.2 a
**liveness** property (something good, termination, eventually happens), in the
sense of [Lam77, AS85]. Termination is obtained *without assuming anything about the
model*: the model may never produce an answer, and the budget still ends the run.

**Proposition 2.3 (Refusal is not termination).** The terminal outcomes of a run are
exactly $\mathsf{answered}$, $\mathsf{step\_budget}$, $\mathsf{cost\_budget}$ and
$\mathsf{aborted}$. A refused request never ends a run.

*Proof.* By Definition 2.5, the loop exits only at the four listed points. A refusal
is a result (Definition 2.4), appended to the history like any other, after which the
loop continues. $\square$

This is deliberate: a refusal is information. The model learns that an action is not
available and can pursue the objective by permitted means, as a client does after a
server rejects one request.

**Proposition 2.4 (Input cost grows quadratically with run length).** If the initial
history has $h$ tokens and each step appends at least $\delta$ tokens, the total
input over $n$ calls is at least $n h + \delta\, n(n-1)/2$.

*Proof.* The $k$-th call's input contains $h_{k-1}$, of at least
$h + (k-1)\delta$ tokens. Summing over $k = 1, \ldots, n$ gives the bound. $\square$

Because the model is stateless (Proposition 1.1), every step re-sends everything
before it; long runs are expensive for a structural reason, and step budgets are an
economic control as much as a safety one. (Providers offer caching of repeated
prefixes, which lowers the price of re-sent input but not its growth.)

## Common misconceptions

- *"The runtime is the model plus its tools."* The model is outside the runtime,
  reached over an interface. The runtime is the loop, the history, the gate and the
  tools; **agent = model + runtime** (Definition 2.1).
- *"A denied request ends the run."* It returns a refusal and the run continues
  (Proposition 2.3). Only the four outcomes of Definition 2.5 end a run.
- *"The model decides which actions happen."* It decides which actions are
  *requested*. The gate decides what happens (Theorem 2.1).
- *"Asking for something dangerous is dangerous."* Asking has no effect
  (Definition 2.3); only execution does.

## In this project (Stage 2, [`02-single-agent/`](../../02-single-agent/))

- `agent.py`: `run()` is Definition 2.5; `execute()` is the gate of Definition 2.4,
  split into `authorize()` (decide: known tool, valid arguments, paths inside the
  workspace, not a runaway repeat) and `perform()` (act: run within a timeout, bound
  the output).
- `tools.py`: five tools with Pydantic argument schemas ($\Sigma_t$) and declared path
  arguments, from which the gate checks the footprint condition of Theorem 2.1.
- `test_agent.py`: 20 cases in which a scripted model requests unknown tools,
  malformed arguments, paths escaping the workspace (including through a symlink),
  and never stops. The test `test_unknown_tool_is_reported_and_the_run_continues` is
  Proposition 2.3. Removing the authorization step makes four tests fail.
- `demo.py`: a live run of five steps whose per-step cost rose from \$0.006 to
  \$0.016 as the history grew (Proposition 2.4).

## Refresher

An agent is a stateless model in a loop run by a trusted runtime. The model proposes
requests; the runtime's gate permits or refuses each one and always answers. Because
every effect passes through the gate, confinement holds against any model output
(safety); because the loop is bounded by steps and spend, every run terminates
(liveness) without trusting the model. Refusals inform, they do not terminate; and
every step pays again for the whole history.

*References:* [Yao23] [Sch23] [SS75] [And72] [Lam77] [AS85]. Full list: [references.md](references.md).
