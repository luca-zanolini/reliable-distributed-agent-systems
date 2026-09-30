"""The software task: one Rust file with two independent bugs, and its test suite.

The tester is a program, not an agent: running tests is deterministic, and
verification should rest on deterministic evidence (Stage 5).
"""

from __future__ import annotations

LIB = """\
/// Fencing: accept a write only if its token is at least as new as the newest seen.
pub fn check(highest_seen: &mut u64, token: u64) -> bool {
    if token >= *highest_seen { *highest_seen = token; true } else { true }
}

/// Leases: every grant carries a strictly increasing fencing token.
pub fn grant(next_token: &mut u64) -> u64 {
    *next_token
}
"""

INITIAL = {"src/lib.rs": LIB, "CHANGELOG.md": "# Changelog\n"}

# What each task asks for, and the (scripted) change an implementer makes for it.
TASKS = {
    "T1": ("src/lib.rs", "fencing: stale tokens must be rejected"),
    "T2": ("src/lib.rs", "leases: every grant must increment the token"),
}

FIXES = {
    "T1": lambda text: text.replace("} else { true }", "} else { false }"),
    "T2": lambda text: text.replace("    *next_token\n}", "    *next_token += 1;\n    *next_token\n}"),
}

# A careless follow-up edit ("tidy up") that silently removes the T2 fix.
REGRESSION = lambda text: text.replace("    *next_token += 1;\n", "")

TESTS = {
    "fencing_accepts_current": lambda lib: "{ *highest_seen = token; true }" in lib,
    "fencing_rejects_stale": lambda lib: "} else { false }" in lib,
    "grant_increments": lambda lib: "*next_token += 1;" in lib,
}


def run_tests(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (passed, failed) test names for a version of the repository."""
    lib = files["src/lib.rs"]
    passed = [n for n, t in TESTS.items() if t(lib)]
    return passed, [n for n in TESTS if n not in passed]
