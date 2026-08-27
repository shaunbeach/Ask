"""Writing generated files, and exporting the conversation.

Both land in the directory `ask` was launched from — the "present directory"
from the user's point of view, since `ask` runs in a pane they cd'd somewhere
first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*[\w.+-]*\s*$")

#: Characters that make a filename awkward to type or portable across systems.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def strip_fences(text: str) -> str:
    """Remove a code fence wrapping the whole reply.

    Asked for a file, a model very often returns it fenced anyway. Writing the
    fence into the file would break the first line of every script — a leading
    ```` ```python ```` is not valid Python.

    Only an *enclosing* fence is removed: a markdown file that legitimately
    contains fenced blocks keeps them, because those do not sit at the very
    start and end of the reply.
    """
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return text.strip()

    opening = FENCE.match(lines[0])
    if not opening:
        return text.strip()
    marker = opening.group(1)[0]

    # Walk back to the last line that closes with the same fence character.
    for i in range(len(lines) - 1, 0, -1):
        closing = FENCE.match(lines[i])
        if closing and closing.group(1)[0] == marker:
            return "\n".join(lines[1:i]).strip("\n")
    # Unterminated — a truncated reply. Drop the opener and keep the rest.
    return "\n".join(lines[1:]).strip("\n")


@dataclass
class Target:
    path: Path
    existed: bool
    backup: Path | None = None


def write_file(name: str, content: str, cwd: Path | None = None) -> Target:
    """Write `content` to `name`, keeping any previous version alongside.

    An existing file is moved to `<name>.bak` rather than refused. Refusing
    would be safer in the abstract, but this is a command people re-run —
    "write it again, shorter" — and a hard error on every retry makes the
    feature useless. Nothing is lost either way.
    """
    base = Path.cwd() if cwd is None else cwd
    path = (base / name).expanduser()
    if not path.is_absolute():
        path = base / path

    existed = path.exists()
    backup: Path | None = None
    if existed:
        backup = path.with_suffix(path.suffix + ".bak")
        path.replace(backup)

    path.parent.mkdir(parents=True, exist_ok=True)
    body = content.rstrip("\n") + "\n"
    path.write_text(body, encoding="utf-8")
    return Target(path=path, existed=existed, backup=backup)


def export_name(label: str, today: date | None = None) -> str:
    """`YYYY-MM-DD-<label>.md`, with the label made safe for a filename."""
    stamp = (today or date.today()).isoformat()
    slug = UNSAFE.sub("-", label.strip()).strip("-._") or "chat"
    if slug.lower().endswith(".md"):
        slug = slug[:-3]
    return f"{stamp}-{slug}.md"


def render_transcript(turns, model: str, provider: str, when=None) -> str:
    """The conversation as markdown.

    Only what was said. Reasoning is excluded for the same reason it never
    reaches the model — it is scratch work, it is long, and it is not the
    answer.
    """
    stamp = (when or date.today()).isoformat()
    out = [f"# ask — {stamp}", "", f"*{model} via {provider}*", ""]
    for turn in turns:
        if turn.role == "user":
            out += ["## " + turn.content.strip().splitlines()[0][:120], ""]
            body = turn.content.strip()
            if len(body.splitlines()) > 1:
                out += [body, ""]
        else:
            out += [turn.content.strip(), ""]
    return "\n".join(out).rstrip() + "\n"
