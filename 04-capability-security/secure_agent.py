"""The Stage 2 loop with capability security.

Differences from Stage 2:

  - Tools come from a separate tool-server process (client.py), not in-process.
  - Only tools the role is granted (allow or ask) AND whose declaration matches its
    pin are shown to the model. A denied tool is never advertised; a tool whose
    description changed since it was pinned is quarantined, because its
    description is text the model will read and obey.
  - Every request is decided by the manifest (policy.py): deny, allow, or ask a
    human approver. With no approver, ask means deny.
  - Every decision is appended to an audit log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "02-single-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "01-llm-runtime"))
from agent import Agent, Budget  # noqa: E402
from client import ToolClient, ToolTimeout  # noqa: E402
from llm import ToolCall, ToolResult, ToolSpec  # noqa: E402
from policy import ALLOW, ASK, DENY, Manifest, declaration_hash  # noqa: E402
from tools import Workspace  # noqa: E402

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict}


def schema_errors(arguments: dict, schema: dict) -> list[str]:
    """The subset of JSON Schema the tool declarations use."""
    props, errors = schema.get("properties", {}), []
    for name in schema.get("required", []):
        if name not in arguments:
            errors.append(f"{name}: required")
    for name, value in arguments.items():
        if name not in props:
            if schema.get("additionalProperties") is False:
                errors.append(f"{name}: not a parameter of this tool")
            continue
        expected = _TYPES.get(props[name].get("type"))
        if expected and (not isinstance(value, expected) or isinstance(value, bool) and expected is not bool):
            errors.append(f"{name}: expected {props[name]['type']}")
    return errors


class SecureAgent(Agent):
    def __init__(self, provider, workspace: Workspace, manifest: Manifest, client: ToolClient,
                 budget: Budget = Budget(), approver=None, audit_log: str | Path | None = None,
                 verbose: bool = False):
        super().__init__(provider, workspace, [], budget, verbose)
        self.manifest, self.client, self.approver = manifest, client, approver
        self.audit_log = Path(audit_log) if audit_log else None
        self.declared, self.quarantined = {}, []
        for t in client.list_tools():
            if manifest.tool_verdict(t["name"]) == DENY:
                continue                                        # never advertised
            if manifest.pins.get(t["name"]) != declaration_hash(t["name"], t["description"], t["inputSchema"]):
                self.quarantined.append(t["name"])              # changed or unpinned: not shown
                continue
            self.declared[t["name"]] = t
        self.specs = [ToolSpec(t["name"], t["description"], t["inputSchema"]) for t in self.declared.values()]

    def _audit(self, call: ToolCall, verdict: str, reason: str):
        if self.audit_log:
            with open(self.audit_log, "a") as f:
                f.write(json.dumps({"tool": call.name, "arguments": call.arguments,
                                    "verdict": verdict, "reason": reason}) + "\n")

    def authorize(self, call: ToolCall, seen: dict[str, int], log):
        # 1. Advertised to this role?
        if call.name not in self.declared:
            self._audit(call, DENY, "not available to this role")
            return self._reply(call, log, "rejected",
                               f"unknown tool {call.name!r}; available: {', '.join(self.declared)}")
        # 2. Arguments match the declared schema?
        errors = schema_errors(call.arguments, self.declared[call.name]["inputSchema"])
        if errors:
            self._audit(call, DENY, "invalid arguments")
            return self._reply(call, log, "rejected", "invalid arguments: " + "; ".join(errors))
        # 3. Permitted by the manifest? (paths, secrets, egress, allow/ask)
        d = self.manifest.decide(call.name, call.arguments, self.ws.root)
        if d.verdict == ASK:
            approved = bool(self.approver and self.approver(call, d.reason))
            self._audit(call, ALLOW if approved else DENY, f"approval {'granted' if approved else 'not granted'}: {d.reason}")
            if not approved:
                return self._reply(call, log, "denied", f"denied: {d.reason}; approval not granted")
        else:
            self._audit(call, d.verdict, d.reason)
            if d.verdict == DENY:
                return self._reply(call, log, "denied", f"denied: {d.reason}")
        # 4. Not a runaway repeat?
        key = call.name + json.dumps(call.arguments, sort_keys=True)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > self.budget.max_identical_calls:
            return self._reply(call, log, "rejected", "refused: identical call already made; use the earlier result")
        return call.name, call.arguments, key

    def perform(self, call: ToolCall, tool, args, log, idempotency_key=None) -> ToolResult:
        try:
            output, is_error = self.client.call(tool, args)
        except ToolTimeout as e:
            return self._reply(call, log, "failed", f"tool {e}")
        except Exception as e:
            return self._reply(call, log, "failed", f"tool failed: {type(e).__name__}: {e}")
        cap = self.budget.max_tool_output_chars
        if len(output) > cap:
            output = output[:cap] + f"\n[truncated: {cap} of {len(output)} characters shown]"
        return self._reply(call, log, "failed" if is_error else "executed", output, is_error=is_error)
