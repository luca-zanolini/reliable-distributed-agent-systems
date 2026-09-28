"""Capability manifests: what a role may do, decided per request.

A manifest names the tools a role may use (allow), may use with a human's
approval (ask), or may not use at all (deny; also the default for anything
unlisted). It additionally constrains two kinds of arguments, whichever tool
carries them:

  - paths: must resolve inside the workspace and must not match a secret pattern;
  - URLs:  must go to a permitted destination (a host allowlist, or, stricter,
           an exact-URL allowlist that leaves the agent no bytes to choose).

Decisions are pure functions of (manifest, request, workspace): the model's
intent, and whatever it was told by the documents it read, play no part.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass(frozen=True)
class Decision:
    verdict: str        # allow | ask | deny
    reason: str


@dataclass
class Manifest:
    role: str
    allow: set[str] = field(default_factory=set)
    ask: set[str] = field(default_factory=set)
    path_arguments: tuple[str, ...] = ("path",)
    secret_patterns: tuple[str, ...] = ()
    url_arguments: tuple[str, ...] = ("url",)
    allow_hosts: tuple[str, ...] = ()
    allow_urls: tuple[str, ...] = ()
    pins: dict[str, str] = field(default_factory=dict)   # tool name -> sha256 of its declaration

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        path = Path(path)
        d = tomllib.loads(path.read_text())
        pins = {}
        if "pins_file" in d:
            pins = json.loads((path.parent / d["pins_file"]).read_text())
        return cls(
            role=d["role"],
            allow=set(d.get("tools", {}).get("allow", [])),
            ask=set(d.get("tools", {}).get("ask", [])),
            path_arguments=tuple(d.get("paths", {}).get("arguments", ["path"])),
            secret_patterns=tuple(d.get("paths", {}).get("secret", [])),
            url_arguments=tuple(d.get("egress", {}).get("arguments", ["url"])),
            allow_hosts=tuple(d.get("egress", {}).get("allow_hosts", [])),
            allow_urls=tuple(d.get("egress", {}).get("allow_urls", [])),
            pins=pins,
        )

    # --- tool level ---------------------------------------------------------

    def tool_verdict(self, name: str) -> str:
        if name in self.allow:
            return ALLOW
        if name in self.ask:
            return ASK
        return DENY                                 # default deny, including unknown tools

    # --- request level ------------------------------------------------------

    def decide(self, name: str, arguments: dict, workspace_root: Path) -> Decision:
        verdict = self.tool_verdict(name)
        if verdict == DENY:
            return Decision(DENY, f"tool {name!r} is not granted to role {self.role!r}")

        for arg in self.path_arguments:
            if arg in arguments:
                d = self._check_path(str(arguments[arg]), workspace_root)
                if d:
                    return d

        for arg in self.url_arguments:
            if arg in arguments:
                d = self._check_url(str(arguments[arg]))
                if d:
                    return d

        if verdict == ASK:
            return Decision(ASK, f"tool {name!r} requires approval for role {self.role!r}")
        return Decision(ALLOW, "granted")

    def _check_path(self, raw: str, root: Path) -> Decision | None:
        root = root.resolve()
        p = (root / raw).resolve()
        if not p.is_relative_to(root):
            return Decision(DENY, f"{raw!r} is outside the workspace")
        rel = p.relative_to(root).as_posix()
        for pattern in self.secret_patterns:
            # fnmatch's '*' also matches '/', so '*.pem' covers every directory.
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
                return Decision(DENY, f"{raw!r} matches secret pattern {pattern!r}")
        return None

    def _check_url(self, url: str) -> Decision | None:
        if self.allow_urls:
            if url in self.allow_urls:
                return None
            return Decision(DENY, f"{url!r} is not an exactly permitted URL")
        parts = urlsplit(url)
        if parts.scheme != "https":
            return Decision(DENY, f"{url!r}: only https is permitted")
        if parts.hostname not in self.allow_hosts:
            return Decision(DENY, f"egress to {parts.hostname!r} is not permitted")
        return None


def declaration_hash(name: str, description: str, input_schema: dict) -> str:
    """What the model is shown about a tool, fingerprinted. A change is a new tool."""
    blob = json.dumps({"name": name, "description": description, "inputSchema": input_schema},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()
