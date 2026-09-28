"""Provider-neutral model adapter.

The rest of the system calls `Provider.complete()` and receives a `Completion`:
a receipt owned by us, not a vendor response object. Only this file knows which
SDK sits behind the interface.

Policy decisions encoded here:

- Transport failures (429, 5xx, timeouts, dropped connections) are retried by
  the SDK with exponential backoff, a fixed number of times. If they persist,
  the adapter raises `TransientError`: retrying later may succeed.
- Request failures (other 4xx) raise `RequestError`: the request itself is
  wrong, and retrying it unchanged cannot succeed.
- Output failures (truncated, refused, schema-invalid) raise `OutputError` and
  are NOT retried here. The model answered and the tokens were billed; whether
  another attempt is worth its cost is the caller's decision. The partial
  receipt travels with the exception so the spend is never lost from accounting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import anthropic
import pydantic


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

# Neutral stop vocabulary. Each provider maps its own stop reasons onto these.
STOP_END = "end"                # the model finished its answer
STOP_TRUNCATED = "truncated"    # cut off by the max_tokens ceiling
STOP_REFUSED = "refused"        # the provider declined to answer
STOP_OTHER = "other"            # anything else (e.g. tool use, in later stages)


@dataclass(frozen=True)
class Completion:
    model: str
    text: str | None                    # free-form answer, or None if structured
    parsed: pydantic.BaseModel | None   # validated object, or None if free-form
    stop: str                           # one of the STOP_* values above
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: float


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Base class: every failure the adapter raises is one of the three below."""


class TransientError(LLMError):
    """Transport failure that persisted through the SDK's retries."""


class RequestError(LLMError):
    """The request was rejected as invalid; retrying it unchanged cannot help."""


class OutputError(LLMError):
    """The model answered, but the answer is unusable. Carries the receipt."""

    def __init__(self, message: str, completion: Completion):
        super().__init__(message)
        self.completion = completion


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class Provider(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        schema: type[pydantic.BaseModel] | None = None,
        max_tokens: int = 1024,
    ) -> Completion: ...


def check(completion: Completion, schema: type[pydantic.BaseModel] | None) -> Completion:
    """Shared output checks, applied by every provider before returning."""
    if completion.stop == STOP_TRUNCATED:
        raise OutputError("output truncated at max_tokens", completion)
    if completion.stop == STOP_REFUSED:
        raise OutputError("provider refused the request", completion)
    if schema is not None and completion.parsed is None:
        raise OutputError(f"output did not validate against {schema.__name__}", completion)
    return completion


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

# USD per million tokens (input, output). Verify against the provider's price
# page before relying on the numbers.
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

_ANTHROPIC_STOPS = {
    "end_turn": STOP_END,
    "stop_sequence": STOP_END,
    "max_tokens": STOP_TRUNCATED,
    "refusal": STOP_REFUSED,
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_PER_MTOK[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class AnthropicProvider:
    def __init__(self, model: str = "claude-opus-4-8", max_retries: int = 2, timeout_s: float = 60.0):
        if model not in PRICES_PER_MTOK:
            raise ValueError(f"no price on record for {model!r}; add it to PRICES_PER_MTOK")
        self.model = model
        self.client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)

    def complete(self, prompt, *, schema=None, max_tokens=1024):
        request = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        t0 = time.perf_counter()
        try:
            if schema is None:
                response = self.client.messages.create(**request)
            else:
                response = self.client.messages.parse(**request, output_format=schema)
        except anthropic.APIConnectionError as e:          # includes timeouts
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            # Same set the SDK itself retries. 409 is transient by this API's
            # convention, not by HTTP semantics; >= 500 also covers 529 (overloaded).
            if e.status_code in (408, 409, 429) or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise RequestError(str(e)) from e
        except pydantic.ValidationError as e:
            # Output arrived but does not fit the schema. There is no response
            # object to take usage from, so the receipt records zero tokens.
            raise OutputError(f"output did not validate: {e}", Completion(
                model=self.model, text=None, parsed=None, stop=STOP_OTHER,
                input_tokens=0, output_tokens=0,
                latency_s=time.perf_counter() - t0, cost_usd=0.0,
            )) from e
        latency = time.perf_counter() - t0

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        completion = Completion(
            model=self.model,
            text=text if schema is None else None,
            parsed=response.parsed_output if schema is not None else None,
            stop=_ANTHROPIC_STOPS.get(response.stop_reason, STOP_OTHER),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_s=latency,
            cost_usd=cost_usd(self.model, usage.input_tokens, usage.output_tokens),
        )
        return check(completion, schema)


# ---------------------------------------------------------------------------
# Fake (deterministic, offline, free)
# ---------------------------------------------------------------------------

class FakeProvider:
    """Replays a script of answers in order. Used by tests and by later stages
    to exercise the runtime deterministically and without cost.

    Each script entry is either a string (the model's raw output; parsed as
    JSON when a schema is requested) or an exception instance, which is raised
    to simulate a provider failure. Token counts are word counts, not real
    tokenizer output; cost is always zero.
    """

    def __init__(self, script: list[str | Exception]):
        self.script = list(script)
        self.prompts: list[str] = []    # every prompt received, for inspection

    def complete(self, prompt, *, schema=None, max_tokens=1024):
        self.prompts.append(prompt)
        if not self.script:
            raise RuntimeError("FakeProvider script exhausted")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry

        output_tokens = len(entry.split())
        stop = STOP_END
        if output_tokens > max_tokens:
            entry = " ".join(entry.split()[:max_tokens])
            output_tokens, stop = max_tokens, STOP_TRUNCATED

        parsed = None
        if schema is not None and stop == STOP_END:
            try:
                parsed = schema.model_validate_json(entry)
            except pydantic.ValidationError:
                parsed = None

        completion = Completion(
            model="fake",
            text=entry if schema is None else None,
            parsed=parsed,
            stop=stop,
            input_tokens=len(prompt.split()),
            output_tokens=output_tokens,
            latency_s=0.0,
            cost_usd=0.0,
        )
        return check(completion, schema)
