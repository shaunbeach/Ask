"""Turn live tmux panes into a context block for the model.

This is the feature the whole app exists for. The budget matters: at an 8192
context window a couple of unfiltered pane dumps will crowd out the
conversation, so every pane is trimmed before it is offered and the assembled
block is capped as a whole.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, replace

from . import tmux
from .config import ContextSettings
from .highlight import Line, Selection, parse as parse_capture
from .tokens import estimate


_HEADER = (
    "Current contents of the user's other terminal panes. This is what they "
    "are looking at right now — use it to ground your answer."
)

_TRUNCATION_NOTE = "\n\n(Older lines were trimmed to fit the context window.)"

#: Blank lines between the header and each pane block. Small, but the budget is
#: only honest if everything `Snapshot.render()` emits is counted — leaving these
#: out let the assembled block finish a few tokens over its cap.
_SEPARATOR_TOKENS = 2


#: A selection longer than this is quoted by reference rather than repeated —
#: at an 8k window, printing a 40-line selection twice is not worth the tokens.
INLINE_SELECTION_CHARS = 400


@dataclass
class PaneCapture:
    pane: tmux.Pane
    text: str
    selection: Selection | None = None

    @property
    def line_count(self) -> int:
        return len(self.text.splitlines())

    def _selection_note(self) -> str:
        """Tell the model what the user is pointing at, if anything."""
        sel = self.selection
        if sel is None:
            return ""
        if sel.kind == "cursor":
            # Deliberately weaker wording: a single full-width highlighted line
            # is where the cursor is, which `cursorline` produces whether or not
            # anything is selected. Claiming a selection here would be a guess.
            return f"\nThe cursor is on {sel.describe()}.\n"
        body = sel.text.strip()
        # Whole lines are cited, not repeated. They are already in the pane dump
        # below with their line numbers, so quoting them again spends tokens
        # twice — and the quoted copy loses its indentation, because a linewise
        # highlight starts at a different column on every line.
        if sel.linewise or len(body) > INLINE_SELECTION_CHARS:
            return (
                f"\nThe user has selected {sel.describe()} — that is very likely "
                f"what they are asking about.\n"
            )
        return (
            f"\nThe user has selected this text in the pane ({sel.describe()}) "
            f"— it is very likely what they are asking about:\n```\n{body}\n```\n"
        )

    def render(self) -> str:
        return (
            f"### tmux {self.pane.label()}"
            f"{self._selection_note()}"
            f"\n```\n{self.text}\n```"
        )


@dataclass
class Snapshot:
    """What we gathered, and what it cost."""

    captures: list[PaneCapture]
    unavailable: str | None = None
    truncated: bool = False

    @property
    def pane_count(self) -> int:
        return len(self.captures)

    @property
    def line_count(self) -> int:
        return sum(c.line_count for c in self.captures)

    def render(self) -> str:
        """The block handed to the model, or empty string if there's nothing."""
        if not self.captures:
            return ""
        body = "\n\n".join(c.render() for c in self.captures)
        note = _TRUNCATION_NOTE if self.truncated else ""
        return f"{_HEADER}\n\n{body}{note}"

    def summary(self) -> str:
        """One-line status for the context bar."""
        if self.unavailable:
            return f"context unavailable — {self.unavailable}"
        if not self.captures:
            return "no other panes"
        panes = "pane" if self.pane_count == 1 else "panes"
        text = f"{self.pane_count} {panes} · {self.line_count} lines"
        return text + " · trimmed" if self.truncated else text


def _clean(lines: list[Line], max_lines: int) -> list[Line]:
    """Collapse blank runs and repeated lines, drop edge blanks, keep the tail.

    Operates on `Line` objects rather than on joined text so each line carries
    its `marked` flag through. Cleaning renumbers everything — collapsing blanks,
    dropping duplicates, and keeping only the tail all shift line positions — and
    a selection anchored to the pre-clean numbering pointed at the wrong lines,
    or past the end of the block entirely. See `_reanchor`.
    """
    collapsed: list[Line] = []
    blanks = 0
    for line in lines:
        stripped = line.text.rstrip()
        if not stripped:
            blanks += 1
            if blanks > 1:
                continue
            collapsed.append(Line(text="", marked=False))
        else:
            blanks = 0
            # An idle shell repeats its prompt forever. Keep one, drop the rest —
            # unless one of them is the selection, which is not noise.
            if collapsed and collapsed[-1].text == stripped and not line.marked:
                continue
            collapsed.append(Line(text=stripped, marked=line.marked))

    while collapsed and not collapsed[0].text:
        collapsed.pop(0)
    while collapsed and not collapsed[-1].text:
        collapsed.pop()

    if len(collapsed) > max_lines:
        collapsed = collapsed[-max_lines:]
    return collapsed


def _reanchor(selection: Selection | None, lines: list[Line]) -> Selection | None:
    """Point a selection at the line numbers the model will actually see.

    Returns None when cleaning or trimming removed every selected line: claiming
    a selection that is no longer in the block is worse than not mentioning one.
    """
    if selection is None:
        return None
    marked = [i + 1 for i, line in enumerate(lines) if line.marked]
    if not marked:
        return None
    return replace(selection, first_line=min(marked), last_line=max(marked))


def _is_idle(text: str) -> bool:
    """True for a pane showing nothing but a bare shell sitting at a prompt.

    Feeding these to the model costs tokens and teaches it nothing, which at an
    8k window is a trade worth refusing.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return True
    # After deduping, a pane whose every line ends in a prompt sigil and which
    # never wrapped to a continuation is just an empty shell.
    prompt_like = sum(1 for ln in lines if re.search(r"[%$#>]\s*$", ln))
    return prompt_like == len(lines)


def gather(
    settings: ContextSettings,
    pinned: str | None = None,
    budget_tokens: int | None = None,
) -> Snapshot:
    """Capture panes, newest-and-most-relevant first, within a token budget.

    `pinned` restricts the capture to a single pane target. Ordering puts the
    active pane first, since that's the one the user just came from and almost
    always the one they're asking about.
    """
    reason = tmux.unavailable_reason()
    if reason:
        return Snapshot(captures=[], unavailable=reason)

    if pinned:
        pane = tmux.resolve(pinned)
        if pane is None:
            return Snapshot(captures=[], unavailable=f"pane {pinned} not found")
        panes = [pane]
    else:
        panes = tmux.list_panes(settings.scope)
        # Active pane first, then the rest in tmux's own order.
        panes.sort(key=lambda p: not p.active)
        panes = panes[: settings.max_panes]

    if not panes:
        return Snapshot(captures=[])

    budget = settings.max_tokens if budget_tokens is None else budget_tokens
    captures: list[PaneCapture] = []
    truncated = False
    # Everything `Snapshot.render()` adds around the pane blocks, reserved up
    # front: the framing header, the separators between blocks, and the
    # truncation note. The note is charged whether or not it ends up being
    # shown — finishing under budget is fine, finishing over it is not.
    spent = estimate(_HEADER) + estimate(_TRUNCATION_NOTE) + _SEPARATOR_TOKENS

    for pane in panes:
        # Parsed once: the same pass strips the colour codes and reads the
        # selection out of them, so the text and the selection cannot disagree.
        captured = parse_capture(tmux.capture(pane.pane_id, settings.lines))
        cleaned = _clean(captured.lines, settings.lines)
        text = "\n".join(line.text for line in cleaned)
        if not text or _is_idle(text):
            continue

        capture = PaneCapture(
            pane=pane, text=text, selection=_reanchor(captured.selection, cleaned)
        )
        cost = estimate(capture.render())

        if spent + cost > budget:
            remaining = budget - spent
            if remaining < 80:
                # Not enough room left to say anything useful about this pane.
                truncated = True
                break
            shrunk = _shrink_to(capture, remaining)
            truncated = True
            if shrunk is None:
                break
            capture = shrunk
            cost = estimate(capture.render())

        captures.append(capture)
        spent += cost + _SEPARATOR_TOKENS

    return Snapshot(captures=captures, truncated=truncated)


def _shrink_to(capture: PaneCapture, budget: int) -> PaneCapture | None:
    """Drop leading lines until the rendered pane fits the remaining budget.

    Measured against `render()` rather than against the text plus a guessed
    header. The header is not the only fixed cost — a selection note sits between
    it and the body — and an overhead estimate that omitted it let the block
    overshoot the budget it was supposed to respect.

    Lines are dropped from the top, so the selection is re-anchored each time.
    """
    lines = [Line(text=t, marked=False) for t in capture.text.splitlines()]
    # Restore the marks so the selection survives the trim.
    sel = capture.selection
    if sel is not None:
        for i in range(sel.first_line - 1, min(sel.last_line, len(lines))):
            if i >= 0:
                lines[i].marked = True

    while lines:
        trial = PaneCapture(
            pane=capture.pane,
            text="\n".join(line.text for line in lines),
            selection=_reanchor(sel, lines),
        )
        if estimate(trial.render()) <= budget:
            return trial
        lines.pop(0)
    return None


def _main() -> int:  # pragma: no cover - manual inspection
    parser = argparse.ArgumentParser(
        prog="python -m ask_tui.context",
        description="Print exactly what ask would send to the model as context.",
    )
    parser.add_argument("--lines", type=int, default=40)
    parser.add_argument("--max-panes", type=int, default=4)
    parser.add_argument("--budget", type=int, default=2000, help="token cap")
    parser.add_argument("--scope", default="window", choices=("window", "session", "all"))
    parser.add_argument("--pane", default=None, help="pin a single pane target")
    args = parser.parse_args()

    settings = ContextSettings(
        lines=args.lines,
        max_panes=args.max_panes,
        scope=args.scope,
        max_tokens=args.budget,
    )
    snap = gather(settings, pinned=args.pane)

    print(f"# {snap.summary()}", file=sys.stderr)
    block = snap.render()
    if block:
        print(f"# ~{estimate(block)} tokens (estimated)\n", file=sys.stderr)
        print(block)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
