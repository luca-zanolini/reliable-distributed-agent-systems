"""Two tools with external side effects, one per non-trivial recovery class.

`send_message` models an external service that accepts an idempotency key and
ignores repeats of a key it has already seen (payment and messaging APIs commonly
do). `append_note` models an action with no such protection: every execution
appends, so executing it twice duplicates its effect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "02-single-agent"))
from tools import Tool, Workspace  # noqa: E402


class SendMessageArgs(BaseModel):
    to: str = Field(description="Recipient name")
    text: str = Field(description="Message body")


def send_message(ws: Workspace, a: SendMessageArgs, idempotency_key: str | None) -> str:
    outbox = ws.root / "outbox.jsonl"
    # Deduplication needs a key: requests without one are all distinct deliveries.
    if idempotency_key is not None and outbox.exists():
        for line in outbox.read_text().splitlines():
            if json.loads(line)["key"] == idempotency_key:
                return f"already delivered (idempotency key {idempotency_key})"
    with open(outbox, "a") as f:
        f.write(json.dumps({"key": idempotency_key, "to": a.to, "text": a.text}) + "\n")
    return f"delivered to {a.to}"


class AppendNoteArgs(BaseModel):
    text: str = Field(description="One line to append to the shared notes log")


def append_note(ws: Workspace, a: AppendNoteArgs) -> str:
    with open(ws.root / "notes.log", "a") as f:
        f.write(a.text + "\n")
    return "appended"


SEND_MESSAGE = Tool("send_message", "Send a message to a named recipient.",
                    SendMessageArgs, send_message, effect="keyed")
APPEND_NOTE = Tool("append_note", "Append one line to the shared notes log.",
                   AppendNoteArgs, append_note, effect="unsafe")
