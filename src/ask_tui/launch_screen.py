"""The startup dialog: pick a model, then start it.

Loading a model pins several gigabytes for a while, so this is always an
explicit choice — and the choice includes *which* model. Starting whichever
entry happened to be first in the config and making the user switch afterwards
means paying that load cost twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from .config import Config, Model
from .server import binary, plan, preflight


@dataclass
class Choice:
    """What the dialog came back with."""

    action: str  # "start" | "connect" | "quit"
    model: Model | None = None


class LaunchScreen(ModalScreen[Choice]):
    """Offers the models in the config, and starts the one you pick."""

    BINDINGS = [("escape", "dismiss_quit", "quit")]

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._models = list(cfg.provider.models)
        # Whatever the config already resolved to is where the cursor starts, so
        # `ask.model` and `--model` still mean "this one" and Enter just takes it.
        self._index = next(
            (i for i, m in enumerate(self._models) if m.name == cfg.model.name), 0
        )
        super().__init__()

    # ---------- helpers ----------

    def _blocker(self, model: Model) -> str | None:
        """Why this particular model can't be launched, if it can't."""
        if binary() is None:
            return "llama-server is not on your PATH"
        probe = Config(
            provider=self._cfg.provider,
            model=model,
            context=self._cfg.context,
            server=self._cfg.server,
            ui=self._cfg.ui,
            system_prompt=self._cfg.system_prompt,
            reply_reserve=self._cfg.reply_reserve,
            source=self._cfg.source,
        )
        return preflight(probe)

    def _row(self, model: Model) -> Text:
        blocker = self._blocker(model)
        text = Text()
        text.append(f"{model.name:<22}", style="#e4e4e7" if not blocker else "#71717a")
        text.append(f"{model.context_window:>7}  ", style="#52525b")
        if blocker:
            # Named rather than hidden: a model whose file has moved should say
            # so here, not fail a minute into a load.
            text.append("missing", style="#f87171")
        else:
            text.append(model.id, style="#52525b")
        return text

    @property
    def _selected(self) -> Model:
        return self._models[self._index]

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        box = Vertical(id="launch-box")
        box.border_title = "choose a model" if len(self._models) > 1 else "no llama-server"
        with box:
            msg = Text()
            msg.append("Nothing is answering at ", style="#e4e4e7")
            msg.append(self._cfg.provider.base_url, style="#c07dff")
            msg.append(".")
            yield Static(msg, id="launch-msg")

            if len(self._models) > 1:
                yield OptionList(
                    *[Option(self._row(m), id=str(i)) for i, m in enumerate(self._models)],
                    id="launch-models",
                )
            else:
                yield Static(self._row(self._models[0]), id="launch-single")

            # The command it would run, condensed. The full argv is forty-odd
            # tokens of sampler flags — printed in full it buried the dialog and
            # pushed the buttons off the bottom.
            yield Static("", id="launch-cmd")

            with Horizontal(id="launch-buttons"):
                yield Button("Start", id="start", classes="-primary")
                yield Button("Connect", id="connect")
                yield Button("Quit", id="quit")

    def on_mount(self) -> None:
        if len(self._models) > 1:
            options = self.query_one("#launch-models", OptionList)
            options.highlighted = self._index
            options.focus()
        else:
            self.query_one("#start", Button).focus()
        self._refresh_cmd()

    def _refresh_cmd(self) -> None:
        model = self._selected
        blocker = self._blocker(model)
        line = Text()
        if blocker:
            line.append(f"⚠ {blocker}", style="#f87171")
        else:
            probe = Config(
                provider=self._cfg.provider, model=model, context=self._cfg.context,
                server=self._cfg.server, ui=self._cfg.ui,
                system_prompt=self._cfg.system_prompt,
                reply_reserve=self._cfg.reply_reserve, source=self._cfg.source,
            )
            launch = plan(probe)
            if launch is not None:
                extra = max(0, len(launch.argv) - 3)
                line.append("will run  ", style="#52525b")
                line.append(f"llama-server -m {launch.model_path.name}", style="#a1a1aa")
                if extra:
                    line.append(f"  +{extra} flags from models.yml", style="#52525b")
        self.query_one("#launch-cmd", Static).update(line)

    # ---------- events ----------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_index is not None:
            self._index = event.option_index
            self._refresh_cmd()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter on the list means "this one" — no second trip to the buttons.
        if event.option_index is not None:
            self._index = event.option_index
        self.dismiss(Choice("start", self._selected))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id or "quit"
        self.dismiss(Choice(action, self._selected if action == "start" else None))

    def action_dismiss_quit(self) -> None:
        self.dismiss(Choice("quit"))
