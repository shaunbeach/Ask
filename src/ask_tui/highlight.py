"""Find what's highlighted in a pane, from the colours tmux reports.

A selection in vim (or less, or a pager) is not text — it is a background colour
painted behind text. `capture-pane -e` keeps those colour codes, so the
information is there to be read; the plain capture otherwise throws it away.

Two things make this tractable:

* **Selection paints the background, syntax highlighting paints the foreground.**
  A selected region arrives wrapped in `48;5;248` (set background); a keyword
  coloured by the syntax file arrives wrapped in `38;5;130` (set foreground).
  Only the former is looked at.

* **A selection is one region; a search highlight is many.** `hlsearch` paints
  every occurrence of the same string, so repeated identical runs are a search,
  not something the user picked out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Any escape sequence. Only SGR ("m") carries colour; the rest — cursor moves,
# erases, OSC titles — is stripped without being interpreted.
ANSI = re.compile(r"\x1b\[([0-9;:]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]")

# Shorter than this is punctuation or a matched bracket, not a selection.
MIN_SPAN = 2

# If nearly every line carries a non-baseline background, the "highlight" is the
# theme rather than anything the user did.
MAX_MARKED_FRACTION = 0.8


@dataclass
class Line:
    text: str
    marked: bool = False


@dataclass
class Selection:
    """What the user appears to have highlighted in a pane.

    `kind` is the honest part. A run *inside* a line, or whole lines plural, can
    only be a selection. A single full-width line cannot be told apart from
    `cursorline`, which vim paints identically and permanently — so rather than
    guess, that case is reported as where the cursor is, which is true whether
    the user selected the line or merely parked on it.
    """

    text: str
    first_line: int
    last_line: int
    kind: str = "selection"  # "selection" | "cursor"
    #: Whole lines were taken, rather than a run inside one.
    linewise: bool = False

    def describe(self) -> str:
        where = (
            f"line {self.first_line}"
            if self.first_line == self.last_line
            else f"lines {self.first_line}\u2013{self.last_line}"
        )
        return f"{where} of the visible pane"


@dataclass
class Parsed:
    lines: list[Line] = field(default_factory=list)
    selection: Selection | None = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _scan(line: str) -> tuple[str, list[str | None]]:
    """Strip escapes from one line, returning its text and each char's background.

    Background state resets per line rather than carrying over: tmux re-emits the
    full colour state at the start of every line, so there is nothing to inherit
    and a stale carry-over would bleed a selection into the line beneath it.
    """
    text: list[str] = []
    bgs: list[str | None] = []
    current: str | None = None

    pos = 0
    for match in ANSI.finditer(line):
        chunk = line[pos : match.start()]
        text.append(chunk)
        bgs.extend([current] * len(chunk))
        pos = match.end()

        if match.group(2) != "m":
            continue
        params = (match.group(1) or "0").split(";")
        i = 0
        while i < len(params):
            p = params[i] or "0"
            if p in ("0", "49"):
                current = None
            elif p == "48":
                # 48;5;N or 48;2;R;G;B — consumed whole, so its numbers are never
                # mistaken for further attributes.
                if i + 1 < len(params) and params[i + 1] == "5":
                    current = ";".join(params[i : i + 3])
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == "2":
                    current = ";".join(params[i : i + 5])
                    i += 4
            elif p.isdigit() and (40 <= int(p) <= 47 or 100 <= int(p) <= 107):
                current = p
            i += 1

    tail = line[pos:]
    text.append(tail)
    bgs.extend([current] * len(tail))
    return "".join(text), bgs


def _spans(text: str, bgs: list[str | None], baseline: str | None) -> list[str]:
    """Contiguous runs of non-baseline background, as their text."""
    out: list[str] = []
    run: list[str] = []
    for ch, bg in zip(text, bgs, strict=True):
        if bg != baseline:
            run.append(ch)
        elif run:
            out.append("".join(run))
            run = []
    if run:
        out.append("".join(run))
    return [s for s in (r.rstrip() for r in out) if len(s) >= MIN_SPAN]


def parse(raw: str) -> Parsed:
    """Split a `-e` capture into lines and work out what's selected."""
    scanned = [_scan(line) for line in raw.splitlines()]

    # The baseline is whatever background most of the pane already uses, not "no
    # background": a themed terminal paints every cell, and comparing against the
    # absence of colour would mark everything.
    counts: dict[str | None, int] = {}
    for _, bgs in scanned:
        for bg in bgs:
            counts[bg] = counts.get(bg, 0) + 1
    baseline = max(counts, key=lambda k: counts[k]) if counts else None

    lines: list[Line] = []
    per_line: list[list[str]] = []
    for text, bgs in scanned:
        spans = _spans(text, bgs, baseline)
        per_line.append(spans)
        lines.append(Line(text=text.rstrip(), marked=bool(spans)))

    substantive = sum(1 for line in lines if line.text.strip())
    marked = sum(1 for line in lines if line.marked)
    if substantive and marked > substantive * MAX_MARKED_FRACTION:
        # The theme, not the user.
        for line in lines:
            line.marked = False
        return Parsed(lines=lines, selection=None)

    flat = [(i + 1, s) for i, spans in enumerate(per_line) for s in spans]
    if not flat:
        return Parsed(lines=lines, selection=None)

    # `hlsearch` paints every occurrence of the same string. Several identical
    # runs are a search, and marking them as "what the user selected" would be
    # confidently wrong.
    distinct = {s.strip() for _, s in flat}
    if len(flat) > 1 and len(distinct) == 1:
        for line in lines:
            line.marked = False
        return Parsed(lines=lines, selection=None)

    numbers = [n for n, _ in flat]
    first, last = min(numbers), max(numbers)
    # A gap means separate highlights — several search hits of differing length,
    # or a pane doing something else entirely. Not one selection.
    if sorted(set(numbers)) != list(range(first, last + 1)):
        for line in lines:
            line.marked = False
        return Parsed(lines=lines, selection=None)

    selected = "\n".join(s for _, s in flat)

    # Does each run reach the end of its line? A highlight that stops mid-line is
    # a deliberate charwise selection; one that runs to the edge is either a
    # linewise selection or vim's cursorline.
    def covers(n: int, span: str) -> bool:
        line = lines[n - 1].text.rstrip()
        run = span.rstrip()
        if not line or not run:
            return False
        return line.endswith(run) and len(run) >= len(line) * 0.4

    full = all(covers(n, s) for n, s in flat)
    kind = "cursor" if full and first == last else "selection"

    return Parsed(
        lines=lines,
        selection=Selection(
            text=selected,
            first_line=first,
            last_line=last,
            kind=kind,
            linewise=full,
        ),
    )
