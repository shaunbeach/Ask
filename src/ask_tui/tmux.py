"""Thin wrapper over the tmux CLI.

Everything that shells out to tmux lives here so the rest of the app can stay
ignorant of format strings and target syntax.

Two situations are supported:
  * running *inside* a tmux pane — we auto-attach the sibling panes of the
    current window and exclude our own pane
  * running outside tmux while a tmux server is up — we can still see every
    pane, so the user pins one with `/pane`
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# Fields we ask tmux for, in order. Tab-separated so paths with spaces survive.
_FORMAT = "\t".join(
    (
        "#{pane_id}",
        "#{session_name}:#{window_index}.#{pane_index}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_width}",
        "#{pane_height}",
        "#{pane_active}",
    )
)


@dataclass
class Pane:
    pane_id: str          # e.g. "%7" — stable, what we capture against
    target: str           # e.g. "2:0.1" — human-readable, what the user types
    command: str
    path: str
    width: int
    height: int
    active: bool

    @property
    def short_path(self) -> str:
        home = os.path.expanduser("~")
        if self.path == home:
            return "~"
        if self.path.startswith(home + "/"):
            return "~" + self.path[len(home):]
        return self.path

    def label(self) -> str:
        return f"{self.target} — {self.command} — {self.short_path} ({self.width}x{self.height})"


def installed() -> bool:
    """True if the tmux binary exists at all."""
    return shutil.which("tmux") is not None


def inside_tmux() -> bool:
    """True if this process is itself running in a tmux pane."""
    return bool(os.environ.get("TMUX")) and installed()


def own_pane_id() -> str | None:
    return os.environ.get("TMUX_PANE")


def _run(args: list[str], timeout: float = 3.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return proc.returncode == 0, proc.stdout


def server_running() -> bool:
    """True if a tmux server is up and answering, session or no session."""
    if not installed():
        return False
    ok, _ = _run(["list-panes", "-a", "-F", "#{pane_id}"])
    return ok


def unavailable_reason() -> str | None:
    """Why pane context can't be gathered, or None if it can."""
    if not installed():
        return "tmux is not installed"
    if not server_running():
        return "no tmux server running"
    return None


def _parse(output: str, exclude_self: bool) -> list[Pane]:
    me = own_pane_id() if exclude_self else None
    panes: list[Pane] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        pane_id, target, command, path, width, height, active = parts
        if me is not None and pane_id == me:
            continue
        try:
            w, h = int(width), int(height)
        except ValueError:
            continue
        panes.append(
            Pane(
                pane_id=pane_id,
                target=target,
                command=command,
                path=path,
                width=w,
                height=h,
                active=active == "1",
            )
        )
    return panes


def list_panes(scope: str = "window") -> list[Pane]:
    """List candidate panes, never including the one we're running in.

    `scope` is "window" (siblings in our window), "session", or "all". When we
    aren't inside tmux there is no "current window" to speak of, so any scope
    degrades to "all".
    """
    if not installed():
        return []

    if not inside_tmux():
        scope = "all"

    flag = {"session": "-s", "all": "-a"}.get(scope)
    args = ["list-panes", "-F", _FORMAT]
    if flag:
        args.insert(1, flag)

    ok, out = _run(args)
    if not ok:
        return []
    return _parse(out, exclude_self=True)


def resolve(target: str) -> Pane | None:
    """Find a pane by the target string a user typed, or by pane id."""
    wanted = target.strip()
    for pane in list_panes(scope="all"):
        if wanted in (pane.pane_id, pane.target):
            return pane
        # Allow the bare "0.1" form when it's unambiguous across sessions.
        if pane.target.split(":", 1)[-1] == wanted:
            return pane
    return None


def capture(pane_id: str, lines: int) -> str:
    """Grab the last `lines` lines of a pane, including scrollback.

    Returns raw output *with* escape sequences — see `-e` below. Callers wanting
    plain text run it through `highlight.parse`, which strips the codes and reads
    the selection out of them on the way past.

    -J joins wrapped lines. Without it anything wider than the pane comes back
    hard-broken mid-word and the model reads garbage.
    -e keeps the colour codes. They are how a selection is represented — vim
    paints a background behind the selected region rather than marking the text
    — so dropping them here would discard the only evidence of what the user is
    pointing at.
    -p prints to stdout instead of stashing a buffer.
    """
    start = f"-{max(lines, 1)}"
    ok, out = _run(
        ["capture-pane", "-p", "-J", "-e", "-S", start, "-t", pane_id], timeout=5.0
    )
    return out if ok else ""
