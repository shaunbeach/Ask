"""Getting a reply out of the TUI and into somewhere you can paste it.

The system prompt tells the model to put every command in its own fenced
block "so it is easy to copy" — but until now the only way to act on that was
to select the text with the mouse, which in a tmux pane means fighting the
pane borders for a multi-line block.

Wayland first: this build is tuned for a Hyprland desktop where `wl-copy` is
always present. The X11 tools stay as a fallback so the module still does
something sensible over a forwarded session, and the tmux buffer is written
alongside the system clipboard whenever we are running inside tmux — that is
the paste target that actually works when `ask` is on the far end of an SSH
connection and the Wayland socket is not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

#: A fence opener, capturing the marker and the optional language tag.
_OPEN = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w.+-]*)\s*$")


@dataclass(frozen=True)
class Tool:
    argv: tuple[str, ...]
    #: Environment variable that must be set for this tool to be usable at all.
    #: `wl-copy` exits non-zero under X11, and `xclip` hangs without a DISPLAY,
    #: so checking the binary exists is not enough on a machine that has both.
    needs: str | None
    label: str


TOOLS = (
    Tool(("wl-copy",), "WAYLAND_DISPLAY", "wl-copy"),
    Tool(("xclip", "-selection", "clipboard"), "DISPLAY", "xclip"),
    Tool(("xsel", "--clipboard", "--input"), "DISPLAY", "xsel"),
)


@dataclass
class Block:
    """One fenced code block from a reply."""

    lang: str
    body: str

    def summary(self, width: int = 48) -> str:
        """First line, trimmed — enough to confirm the right block was taken."""
        first = self.body.strip().splitlines()[0] if self.body.strip() else ""
        return first[: width - 1] + "…" if len(first) > width else first


def code_blocks(text: str) -> list[Block]:
    """Every fenced block in `text`, in order.

    Deliberately tolerant of an unterminated final fence: replies get cut off
    by `escape` or a context limit mid-block, and the half a command that did
    arrive is usually still what the user wanted.
    """
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        opening = _OPEN.match(lines[i])
        if not opening:
            i += 1
            continue
        marker, lang = opening.group(1)[0], opening.group(2)
        body: list[str] = []
        i += 1
        while i < len(lines):
            closing = _OPEN.match(lines[i])
            if closing and closing.group(1)[0] == marker and not closing.group(2):
                i += 1
                break
            body.append(lines[i])
            i += 1
        if body:
            blocks.append(Block(lang=lang, body="\n".join(body).strip("\n")))
    return blocks


def detect() -> Tool | None:
    """The clipboard tool we should use here, or None if there isn't one."""
    for tool in TOOLS:
        if tool.needs and not os.environ.get(tool.needs):
            continue
        if shutil.which(tool.argv[0]):
            return tool
    return None


def _to_tmux_buffer(text: str) -> bool:
    """Also stash it in the tmux paste buffer, if we're inside tmux."""
    if not os.environ.get("TMUX") or not shutil.which("tmux"):
        return False
    try:
        proc = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=text,
            text=True,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def copy(text: str) -> tuple[bool, str]:
    """Put `text` on the clipboard. Returns (ok, a line to show the user).

    Never raises: a failed copy is a notice in the log, not a crash of the
    conversation you were in the middle of.
    """
    if not text.strip():
        return False, "Nothing to copy."

    tool = detect()
    tmuxed = _to_tmux_buffer(text)

    if tool is None:
        if tmuxed:
            return True, "Copied to the tmux buffer (`prefix ]` to paste) — no system clipboard tool found."
        return False, (
            "No clipboard tool available. Install `wl-clipboard` for Wayland "
            "or `xclip` for X11."
        )

    # stderr goes to a temp file, never a pipe. `wl-copy` forks a daemon to
    # serve the selection and that daemon inherits our descriptors — with a
    # pipe, `run()` blocks until EOF that only arrives when the *selection* is
    # replaced, so a copy that plainly worked would report a timeout. A regular
    # file lets the parent exit immediately and still leaves us the diagnostics.
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
            proc = subprocess.run(
                list(tool.argv),
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=err,
                timeout=5.0,
                check=False,
            )
            err.seek(0)
            stderr = err.read()
    except subprocess.TimeoutExpired:
        return False, f"`{tool.label}` timed out."
    except OSError as exc:
        return False, f"Couldn't run `{tool.label}`: {exc}"

    if proc.returncode != 0:
        detail = stderr.strip().splitlines()
        why = f" — {detail[0]}" if detail else ""
        return False, f"`{tool.label}` failed{why}"

    where = f"{tool.label} + tmux buffer" if tmuxed else tool.label
    return True, f"Copied to clipboard ({where})."
