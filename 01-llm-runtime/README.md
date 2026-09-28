# Stage 1 — LLM Runtime

A model call treated as an engineered component: a stateless function behind a
provider-neutral interface, returning a receipt the application owns.

## Files

| File | Purpose |
|---|---|
| `call.py` | A single request; typed content blocks, stop reason, token usage. |
| `amnesia.py` | Statelessness: separate calls share nothing; replaying history restores context. Conversation state lives in the application. |
| `structured.py` | Schema-constrained output: untrusted log text in, a validated `TestReport` out. |
| `llm.py` | The adapter: `Provider` interface, `Completion` receipt, error taxonomy, `AnthropicProvider`, `FakeProvider`. |
| `measure.py` | Free-form vs structured output, N runs each: tokens, latency, cost, distinct answers. |
| `test_llm.py` | Adapter contract tests. Offline and free (`FakeProvider`). |

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python -m unittest -v          # offline, no API key needed
python measure.py 10           # 20 live calls, ~$0.12 on claude-opus-4-8
```

## The adapter contract

Application code calls `provider.complete(prompt, schema=None, max_tokens=...)` and
receives a `Completion`. Nothing outside `llm.py` imports a vendor SDK.

**The receipt.** Every call yields `model`, `text` *or* `parsed`, a neutral `stop`
value, `input_tokens`, `output_tokens`, `latency_s`, and `cost_usd`. Vendor stop
reasons are mapped onto a four-word vocabulary (`end`, `truncated`, `refused`,
`other`), so callers never branch on provider-specific strings.

**Failure taxonomy and retry policy.**

| Failure | Raised as | Retried by adapter? | Rationale |
|---|---|---|---|
| 429, 5xx, timeout, connection drop | `TransientError` | Yes: SDK exponential backoff, bounded (`max_retries`); raised once exhausted | The request is sound; the service or network is not, momentarily. |
| Other 4xx | `RequestError` | No | The request is wrong; repeating it cannot succeed. |
| Truncated, refused, or schema-invalid output | `OutputError` (carries the receipt) | No | The model answered and the tokens were billed. Whether another attempt is worth its cost is a decision for the caller, who knows the budget. |

Two properties follow. A truncated answer is an error, never a silently short
answer: truncation returns HTTP 200 and would otherwise pass as a complete
response. And spend is never lost from accounting: an `OutputError` carries the
receipt of the call that produced it.

**`FakeProvider`** replays a script of outputs and failures, deterministically and
at zero cost. It is the test double for this stage and the substrate for testing
the agent loop in later ones. `measure.py` runs unchanged against it, which is
the check that the interface is actually provider-neutral.

## Experiment: free-form vs structured output

**Setup.** One extraction task: summarise a five-test `cargo test` log with one
failure (`fencing::stale_writer_rejected`). Two arms: a free-form summary request,
and the same log extracted into a three-field schema
(`passed: bool, tests_run: int, failures: list[str]`) under constrained decoding.
Model `claude-opus-4-8`; three independent batches of 10 sequential calls per arm
(N = 30 per arm). Latency is client-side wall-clock, network included.

**Results.**

| | Free-form (N=30) | Structured (N=30) |
|---|---|---|
| Input tokens | 154 (all runs) | 456 (all runs) |
| Output tokens | 265–405, mean 317 (σ ≈ 35) | 56 (all runs) |
| Latency | 4.43–7.39 s, median 4.97 s | 1.59–3.60 s, median 1.92 s |
| Cost per call | ≈ $0.0087 | $0.00368 |
| Distinct answers | 10/10 in the batch inspected | 1/30 |

**Findings.**

1. **Input is deterministic; output is not.** Input token count is a function of the
   request alone. Output length varied by a factor of 1.5 on identical requests.
2. **Structured output bought stability, speed and cost at once.** The schema tripled
   input (it travels with the request) yet cost 2.4× less per call, because output is
   priced at 5× input on this model and output fell by 5.7×. Budget output tokens first.
3. **Variation sat where the model had choices.** In the free-form arm, the facts
   copied from the log (verdict, counts, test name, assertion text) were identical in
   every inspected run; headings and interpretation varied. The open `str` field in the
   schema did not vary: copying a line verbatim leaves the model nothing to choose.
   Constraining the type is necessary for stability, but not by itself sufficient.
4. **Unsupported claims appeared only in the unconstrained part.** Individual free-form
   runs asserted a root-cause diagnosis, or that the passing mechanisms "are working
   as expected", neither of which the log supports and neither of which recurred. The
   schema has no field to carry such claims; what may enter the system is a design
   decision, made in the schema.
5. **Latency ≈ output length × per-token rate + noise.** Output tokens and latency
   correlate (r = 0.65 over the free-form runs, roughly 15 ms per output token). The
   structured arm isolates the noise: identical 56-token outputs took 1.59–3.60 s,
   a spread of more than 2× for identical work.
6. **Truncation is silent unless checked.** With a 300-token ceiling, one free-form run
   stopped at exactly 300 tokens with `stop_reason = max_tokens` and no error. The
   adapter now raises on truncation.

Sampling temperature is not a lever here: current models reject the parameter.
Nondeterminism is confined by the interface, not configured away.

**Limitations.** One task, one model, N = 30 on a single day. Stability of structured
output on a copy-style extraction does not transfer to fields that require judgement.

## Exit criterion

Conversation state is owned by the application (`amnesia.py`); the model call is a
stateless function with a receipt (`llm.py`), behind an interface that runs
unchanged on a deterministic fake.
