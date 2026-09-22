import anthropic
from pydantic import BaseModel

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

client = anthropic.Anthropic()

response = client.messages.parse(
    model="claude-opus-4-8",
    max_tokens=500,
    messages=[{"role": "user", "content": f"Extract a test report from this log:\n{FAKE_LOG}"}],
    output_format=TestReport,
)

report = response.parsed_output      # a real TestReport instance, already validated
print(f"passed={report.passed}  tests_run={report.tests_run}")
print(f"failures={report.failures}")
print(type(report))
