"""The actions an agent may request, confined to one disposable workspace.

A tool is an ordinary Python function plus a declaration the model can read: a
name, a description, and a Pydantic model of its arguments (from which the JSON
Schema sent to the model is generated, and against which every incoming request
is validated). Tools also declare which of their arguments are paths, so the
runtime can authorize them before anything runs.
"""

from __future__ import annotations

import ast
import operator
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field


class Workspace:
    """A directory the agent may touch. Nothing outside it is reachable."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        # resolve() collapses '..' and follows symlinks, so escapes by either
        # route end up outside root and are refused. Absolute paths replace root.
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root):
            raise PermissionError(f"{path!r} is outside the workspace")
        return p


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Workspace, BaseModel], str]
    path_args: tuple[str, ...] = ()     # argument names the runtime must authorize


# --- read_file --------------------------------------------------------------

class ReadFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the workspace root")


def read_file(ws: Workspace, a: ReadFileArgs) -> str:
    return ws.resolve(a.path).read_text(encoding="utf-8")


# --- list_dir ---------------------------------------------------------------

class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory, relative to the workspace root")


def list_dir(ws: Workspace, a: ListDirArgs) -> str:
    d = ws.resolve(a.path)
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in d.iterdir())
    return "\n".join(entries) or "(empty directory)"


# --- search -----------------------------------------------------------------

class SearchArgs(BaseModel):
    text: str = Field(min_length=1, description="Literal text to find (not a regex)")
    path: str = Field(default=".", description="Directory to search, relative to the workspace root")


MAX_MATCHES = 50


def search(ws: Workspace, a: SearchArgs) -> str:
    base = ws.resolve(a.path)
    hits = []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or not f.resolve().is_relative_to(ws.root):
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue                                    # skip binary/unreadable files
        for n, line in enumerate(lines, 1):
            if a.text in line:
                hits.append(f"{f.relative_to(ws.root)}:{n}: {line.strip()}")
                if len(hits) == MAX_MATCHES:
                    return "\n".join(hits) + f"\n[stopped at {MAX_MATCHES} matches]"
    return "\n".join(hits) or "no matches"


# --- calculate --------------------------------------------------------------

class CalculateArgs(BaseModel):
    expression: str = Field(max_length=200, description="Arithmetic only: numbers, + - * / // % ** and parentheses")


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    # Walk the parsed expression and allow only numbers and arithmetic. Names,
    # calls, attributes and everything else are rejected: this is not eval().
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")      # 9**9**9 would hang the runtime
        return _OPS[type(node.op)](left, right)
    raise ValueError(f"not allowed in an arithmetic expression: {type(node).__name__}")


def calculate(ws: Workspace, a: CalculateArgs) -> str:
    return str(_eval(ast.parse(a.expression, mode="eval")))


# --- write_file -------------------------------------------------------------

class WriteFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the workspace root")
    content: str = Field(description="Complete new contents of the file")


def write_file(ws: Workspace, a: WriteFileArgs) -> str:
    # Atomic replace: write a temporary file, then rename it over the target.
    # A failure at any point leaves either the old file or the new one, never a
    # half-written mixture.
    target = ws.resolve(a.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(a.content, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return f"wrote {len(a.content)} characters to {a.path}"


# --- registry ---------------------------------------------------------------

READ_ONLY = [
    Tool("read_file", "Return the contents of a text file in the workspace.", ReadFileArgs, read_file, ("path",)),
    Tool("list_dir", "List the entries of a directory in the workspace.", ListDirArgs, list_dir, ("path",)),
    Tool("search", "Find lines containing a literal text in files under a directory.", SearchArgs, search, ("path",)),
    Tool("calculate", "Evaluate an arithmetic expression exactly.", CalculateArgs, calculate),
]

WRITE_FILE = Tool("write_file", "Create or overwrite a text file in the workspace.",
                  WriteFileArgs, write_file, ("path",))
