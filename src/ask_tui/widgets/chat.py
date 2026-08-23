"""Chat turns.

Assistant turns arrive token by token. Re-rendering markdown on every token is
far more work than a terminal needs, so the widget accumulates raw text and
repaints on a timer instead.
"""

from __future__ import annotations

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
        super().__init__(**kwargs)
        self.add_class(role)

    def on_mount(self) -> None:
        self.update(self._build())

    @property
    def raw(self) -> str:
        return self._raw

    def _build(self) -> RenderableType:
        if self.role == "user":
            return Text(f"❯ {self._raw}", style="#c07dff")
        if self.role in ("notice", "error"):
            return Text(self._raw)
        if not self._raw:
            return Text("…", style="#52525b") if not self._note else Text(
                self._note, style="#52525b"
            )
        body = Markdown(self._raw, code_theme="ansi_dark", inline_code_theme="ansi_dark")
        if not self._note:
            return body
        return Group(body, Text(self._note, style="#52525b"))



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

    def _flush(self) -> None:
        if not self._pending:
            return
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
        self._cancelled = cancelled
        if cancelled and not self._note:
            self._note = "*(stopped)*"
            self.update(self._build())

    class Grew(Message):
        """Emitted after a repaint so the log can follow the tail."""
