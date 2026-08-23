"""The input.

A single-line Input scrolls horizontally, which means in a narrow pane you can
only ever see one line of your own question — the rest runs off the edge. This
wraps instead, growing downward as you type and capping before it eats the
conversation.
"""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea

# Terminals disagree about shift+enter, so offer several ways to get a newline.
NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")


class PromptArea(TextArea):
    """A wrapping, auto-growing prompt. Enter sends; alt+enter adds a line."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(soft_wrap=True, compact=True, placeholder=placeholder, **kwargs)

    async def _on_key(self, event: events.Key) -> None:
        # TextArea maps enter to "insert a newline"; here it means "send".
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
            return
        if event.key in NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    def clear(self) -> None:
        self.text = ""
