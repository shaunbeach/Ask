"""Chat turns.

Assistant turns arrive token by token. Re-rendering markdown on every token is
far more work than a terminal needs, so the widget accumulates raw text and
repaints on a timer instead.
"""

from __future__ import annotations

from time import monotonic

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.message import Message
from textual.widgets import Static

# Repaint at ~20fps while streaming. Fast enough to feel live, slow enough that
# the compositor isn't doing markdown parsing on every single token.
REPAINT_INTERVAL = 0.05


class ChatTurn(Static):
    """One message in the log."""

    def __init__(self, role: str, content: str = "", **kwargs) -> None:
        self.role = role
        self._raw = content
        # INVARIANT: every attribute `_build` reads is initialised here, on the
        # base class — not on AssistantTurn. `_build` is shared by both, so a
        # subclass-only attribute crashes any plain ChatTurn that renders, which
        # is every `/help`, `/model`, `/models`, `/think` and error notice.
        # This has broken twice; add new render state here, never below.
        self._note = ""
        self._reasoning = ""
        self._think_started: float | None = None
        self._think_seconds: float | None = None
        super().__init__(**kwargs)
        self.add_class(role)

    def on_mount(self) -> None:
        self.update(self._build())

    @property
    def raw(self) -> str:
        return self._raw

    def _think_line(self) -> Text | None:
        if self._think_started is None:
            return None
        if self._think_seconds is None:
            elapsed = monotonic() - self._think_started
            return Text(f"⋯ thinking… {elapsed:.1f}s", style="#7c7c86")
        return Text(f"⋯ thought for {self._think_seconds:.1f}s", style="#52525b")

    def _build(self) -> RenderableType:
        if self.role == "user":
            return Text(f"❯ {self._raw}", style="#c07dff")
        if self.role in ("notice", "error"):
            return Text(self._raw)
        think = self._think_line()
        if not self._raw:
            if think is not None:
                return think
            return Text(self._note or "…", style="#52525b")
        body = Markdown(self._raw, code_theme="ansi_dark", inline_code_theme="ansi_dark")
        parts = [p for p in (think, body, Text(self._note, style="#52525b") if self._note else None) if p is not None]
        return parts[0] if len(parts) == 1 else Group(*parts)

    def set_content(self, content: str) -> None:
        self._raw = content
        self.update(self._build())


class AssistantTurn(ChatTurn):
    """A streaming assistant reply."""

    def __init__(self, **kwargs) -> None:
        super().__init__("assistant", "", **kwargs)
        self._pending = ""
        self._timer = None
        self._cancelled = False
        # Rendered after the reply but kept out of `raw`. It used to be appended
        # to the text itself, which meant the literal string "*(stopped)*" was
        # recorded into the conversation and sent back to the model on the next
        # turn as though the assistant had written it.
        self._note = ""

    def start_stream(self) -> None:
        self._timer = self.set_interval(REPAINT_INTERVAL, self._flush)

    def feed(self, piece: str) -> None:
        self._pending += piece

    @property
    def reasoning(self) -> str:
        return self._reasoning

    @property
    def thought_seconds(self) -> float | None:
        return self._think_seconds

    def feed_reasoning(self, piece: str) -> None:
        """Scratch work from a thinking model."""
        if self._think_started is None:
            self._think_started = monotonic()
        self._reasoning += piece

    def _stop_thinking(self) -> None:
        if self._think_started is not None and self._think_seconds is None:
            self._think_seconds = monotonic() - self._think_started

    def _flush(self) -> None:
        # Repaint while thinking too, so the elapsed counter ticks even before a
        # single token of the answer exists.
        if not self._pending:
            if self._think_started is not None and self._think_seconds is None:
                self.update(self._build())
            return
        # The first real token is where thinking ended.
        self._stop_thinking()
        self._raw += self._pending
        self._pending = ""
        self.update(self._build())
        self.post_message(self.Grew())

    def finish(self, cancelled: bool = False) -> None:
        """Stop streaming. Safe to call twice — the error path already may have."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._flush()
        self._stop_thinking()
        self._cancelled = cancelled
        if cancelled and not self._note:
            self._note = "*(stopped)*"
            self.update(self._build())

    class Grew(Message):
        """Emitted after a repaint so the log can follow the tail."""
