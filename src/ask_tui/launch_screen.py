"""The 'no server is running — want me to start one?' dialog.

Loading a model pins GPU memory for a while, so this is always an explicit
choice. The exact command is shown before anything is spawned.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .config import Config
from .server import LaunchPlan


class LaunchScreen(ModalScreen[str]):
    """Returns one of: 'start', 'connect', 'quit'."""

    BINDINGS = [("escape", "dismiss_quit", "quit")]

    def __init__(self, cfg: Config, launch: LaunchPlan | None, blocker: str | None) -> None:
        self._cfg = cfg
        self._launch = launch
        self._blocker = blocker
        super().__init__()

    def compose(self) -> ComposeResult:
        box = Vertical(id="launch-box")
        box.border_title = "no llama-server"
        with box:
            msg = Text()
            msg.append("Nothing is answering at ", style="#e4e4e7")
            msg.append(self._cfg.provider.base_url, style="#c07dff")
            msg.append(".\n")
            if self._blocker:
                msg.append(f"\nCan't start one here: {self._blocker}", style="#f87171")
            else:
                msg.append("\nI can start one with:", style="#a1a1aa")
            yield Static(msg, id="launch-msg")

            if self._launch is not None and not self._blocker:
                cmd = Text(self._launch.command_line(), style="#52525b")
                cmd.no_wrap = False
                yield Static(cmd, id="launch-cmd")

            with Horizontal(id="launch-buttons"):
                if self._launch is not None and not self._blocker:
                    yield Button("Start", id="start", classes="-primary")
                yield Button("Connect", id="connect")
                yield Button("Quit", id="quit")

    def on_mount(self) -> None:
        target = "#start" if self._launch and not self._blocker else "#connect"
        self.query_one(target, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "quit")

    def action_dismiss_quit(self) -> None:
        self.dismiss("quit")
