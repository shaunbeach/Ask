"""The two thin bars that frame the input: what's attached, and what it costs."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.widgets import Static

from ..context import Snapshot
from ..session import Budget


class ContextBar(Static):
    """Shows exactly which panes are riding along with the next message.

    This is deliberately always visible: the app reads the user's terminal, and
    it should never be a mystery what it picked up.
    """

    def show(self, snap: Snapshot, pinned: str | None, enabled: bool) -> None:
        self.remove_class("pinned", "off")
        if not enabled:
            self.add_class("off")
            self.update(Text("○ context off", style="#52525b"))
            return
        if snap.unavailable:
            self.add_class("off")
            self.update(Text(f"○ {snap.summary()}", style="#52525b"))
            return
        if pinned:
            self.add_class("pinned")
            text = Text(f"◉ pinned {pinned} · {snap.line_count} lines")
        else:
            targets = ", ".join(c.pane.target for c in snap.captures)
            label = f"◉ {snap.summary()}" + (f"  ({targets})" if targets else "")
            text = Text(label)
        self._append_selection(text, snap)
        self.update(text)

    @staticmethod
    def _append_selection(text: Text, snap: Snapshot) -> None:
        """Say when a selection is riding along.

        Surfaced because it changes the answer: the model is told the selection
        is what the question is about, so it must not be a surprise that one was
        picked up. Applies to a pinned pane as much as to an auto-attached one —
        the marker lived only on the auto branch at first, and pinning a pane
        silently hid it.
        """
        found = next((c for c in snap.captures if c.selection is not None), None)
        if found is None or found.selection is None:
            return
        sel = found.selection
        label = "selection" if sel.kind == "selection" else "cursor"
        text.append(f"  ✦ {label}", style="#67e8f9")


class StatusBar(Horizontal):
    """Key hints on the left, context-window meter on the right.

    The meter is sized to its content and the hints take the remainder, so on a
    narrow pane the hints give way and the meter — the part that is actually
    live information — always survives.

    Width comes from the Resize event rather than from `self.size`: during a
    resize the widget's own size is still the pre-resize one, which is what made
    an earlier version paint the meter off the right edge.
    """

    HINTS = "^C quit · ^L clear · esc stop · /help"
    SHORT_HINTS = "^C · ^L · esc · /help"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._budget = Budget()
        self._exact = True
        self._width = 0

    def compose(self):
        yield Static("", id="hints")
        yield Static("", id="meter")

    def on_resize(self, event: events.Resize) -> None:
        self._width = event.size.width
        self._paint()

    def show(self, budget: Budget) -> None:
        self._budget = budget
        self._exact = budget.exact
        self._paint()

    def _paint(self) -> None:
        if not self.is_mounted:
            return
        # "~" marks an estimate, but nothing has been estimated at zero — the
        # marker there just looks like a glitch.
        approximate = not self._exact and self._budget.used > 0
        meter = self._budget.human() + (" ~" if approximate else "")
        if self._budget.pressure > 0.9:
            colour = "#f87171"
        elif self._budget.pressure > 0.7:
            colour = "#fbbf24"
        else:
            colour = "#52525b"

        width = self._width or self.size.width
        room = width - len(meter) - 2
        if not width:
            hints = self.HINTS
        elif len(self.HINTS) <= room:
            hints = self.HINTS
        elif len(self.SHORT_HINTS) <= room:
            hints = self.SHORT_HINTS
        else:
            hints = ""

        self.query_one("#hints", Static).update(Text(hints, style="#52525b"))
        self.query_one("#meter", Static).update(Text(meter, style=colour))
