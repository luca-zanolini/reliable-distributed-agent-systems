import anthropic
from pydantic import BaseModel
import time

class TestReport(BaseModel):
    passed: bool
    tests_run: int
    failures: list[str]

FAKE_LOG = """
running 5 tests
test quorum::intersection ... ok
test leases::expiry ... ok
test fencing::stale_writer_rejected ... FAILED
test ledger::no_double_assign ... ok
test retry::idempotent ... ok

failures: fencing::stale_writer_rejected — assertion failed: expected Rejected, got Accepted
test result: FAILED. 4 passed; 1 failed
"""

PRICE_IN_PER_MTOK = 5.00
PRICE_OUT_PER_MTOK = 25.00

client = anthropic.Anthropic()

print("== free-form ==")

for i in range(10):

    t0 = time.perf_counter()

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1000,
        messages=[{"role": "user", "content": f"Give me a summary of the log:\n{FAKE_LOG}"}],
    )

    latency = time.perf_counter() - t0
    cost = response.usage.input_tokens * (PRICE_IN_PER_MTOK/1000000) + response.usage.output_tokens * (PRICE_OUT_PER_MTOK/1000000)

    # for block in response.content:
    #     if block.type == "text":
    #         print(block.text)

    print(f"run={i} Latency: {latency:.2f} in={response.usage.input_tokens} out={response.usage.output_tokens} cost=${cost:.6f} stop={response.stop_reason}")

print("== structured ==")

for i in range(10):

    t0 = time.perf_counter()

    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=500,
        messages=[{"role": "user", "content": f"Extract a test report from this log:\n{FAKE_LOG}"}],
        output_format=TestReport,
    )

    latency = time.perf_counter() - t0
    cost = response.usage.input_tokens * (PRICE_IN_PER_MTOK/1000000) + response.usage.output_tokens * (PRICE_OUT_PER_MTOK/1000000)

    print(response.parsed_output)

    print(f"run={i} Latency: {latency:.2f} in={response.usage.input_tokens} out={response.usage.output_tokens} cost=${cost:.6f} stop={response.stop_reason}")
