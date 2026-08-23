"""The Textual application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from . import __version__, tmux
from .client import ChatError
from .config import Config
from .context import Snapshot
from .launch_screen import LaunchScreen
from .server import ManagedServer, plan, preflight
from .session import Session
from .widgets.bars import ContextBar, StatusBar
from .widgets.chat import AssistantTurn, ChatTurn
from .widgets.prompt import PromptArea
from .widgets.splash import Splash

HELP = """\
**Talking to it** — just type. The contents of your other tmux panes ride along
automatically; the bar above the input shows exactly what's attached.

**Commands**

| | |
|---|---|
| `/panes` | list panes it can see |
| `/pane <target>` | attach only that pane, e.g. `/pane 2:0.1` |
| `/pane off` | go back to attaching every sibling pane |
| `/context on\\|off` | toggle pane context entirely |
| `/context <n>` | lines to take from each pane |
| `/clear` | forget the conversation |
| `/model` | show the model and where it came from |
| `/system` | show the active system prompt |
| `/help` | this |
| `/quit` | leave |

**Keys** — `enter` sends, `alt+enter` adds a line, `esc` stops a reply,
`^L` clears, `^C` quits. Long questions wrap; the box grows as you type.
"""


class AskApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "ask"

    # Required for `ansi_default` to survive as "emit no background code". Without
    # it Textual resolves that token to an opaque grey and the terminal never
    # shows through — the flag and the token only work as a pair.
    ansi_color = True

    BINDINGS = [
        ("ctrl+c", "quit", "quit"),
        ("ctrl+l", "clear", "clear"),
        ("escape", "stop", "stop"),
    ]

    def __init__(self, cfg: Config, pinned_pane: str | None = None) -> None:
        # Before `super().__init__()`: it builds the stylesheet, which calls
        # `get_css_variables()` below, which needs the config to know whether
        # `$surface` should be the terminal's background or ours.
        self.cfg = cfg
        super().__init__()
        self.ansi_color = cfg.ui.transparent
        self.session = Session.create(cfg)
        self.session.pinned_pane = pinned_pane
        self.managed = ManagedServer(cfg)
        self.exit_code = 0
        self._http: httpx.AsyncClient | None = None
        self._streaming = False
        self._connected = False

    def get_css_variables(self) -> dict[str, str]:
        """Resolve `$surface` to either the terminal's background or our own.

        Overridden here rather than branching in the stylesheet: every painted
        surface already reads `$surface`, so opacity is one value, not a parallel
        set of rules to keep in step.
        """
        variables = super().get_css_variables()
        variables["surface"] = "ansi_default" if self.cfg.ui.transparent else "#09090b"
        return variables

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        frame = Vertical(id="frame")
        frame.border_title = f"ask v{__version__}"
        with frame:
            with VerticalScroll(id="body"):
                yield Splash(self.cfg.model.name, self.cfg.provider.key)
            yield ContextBar(id="contextbar")
            with Horizontal(id="promptrow"):
                yield Static("❯", id="promptmark")
                yield PromptArea(
                    placeholder="Ask about a command, an error, a file…",
                    id="promptbar",
                )
            yield StatusBar(id="statusbar")

    async def on_mount(self) -> None:
        self._http = httpx.AsyncClient()
        self.query_one("#promptbar", PromptArea).focus()
        self.call_after_refresh(self.refresh_bars)
        self.connect()

    async def on_unmount(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        self.managed.stop()

    # ---------- server connection ----------

    @work
    async def connect(self) -> None:
        assert self._http is not None
        if await self.session.client.health(self._http):
            self._connected = True
            await self._adopt_server_model()
            return

        if not self.cfg.server.offer_launch:
            self.notice("No llama-server answering. Start one, then send a message.", error=True)
            return

        blocker = preflight(self.cfg)
        launch = plan(self.cfg)
        choice = await self.push_screen_wait(LaunchScreen(self.cfg, launch, blocker))

        if choice == "quit":
            self.exit()
            return
        if choice == "connect" or launch is None or blocker:
            self.notice("Not connected — send a message once a server is up.", error=True)
            return

        self.notice(f"Starting llama-server… (log: {launch.log_path})")
        try:
            self.managed.start(launch)
        except OSError as exc:
            self.notice(f"Couldn't start llama-server: {exc}", error=True)
            return

        ready = await self.managed.wait_until_ready(
            self._http, self.cfg.provider.base_url, self.cfg.server.startup_timeout
        )
        if ready:
            self._connected = True
            await self._adopt_server_model()
            self.notice(f"Connected to {self.cfg.provider.base_url}")
        else:
            tail = self.managed.tail_log(12)
            detail = f"\n\n```\n{tail}\n```" if tail else ""
            self.notice(f"llama-server didn't come up in time.{detail}", error=True)

    async def _adopt_server_model(self) -> None:
        """Show whatever the server actually has loaded, not just what we asked for."""
        assert self._http is not None
        name = await self.session.client.server_model_name(self._http)
        if not name:
            return
        short = name.rsplit("/", 1)[-1]
        if short and short != self.cfg.model.id:
            for splash in self.query(Splash):
                splash.refresh_labels(short, self.cfg.provider.key)

    # ---------- log helpers ----------

    @property
    def body(self) -> VerticalScroll:
        return self.query_one("#body", VerticalScroll)

    def _drop_splash(self) -> None:
        for splash in self.query(Splash):
            splash.remove()

    def append(self, widget, replaces_splash: bool = True) -> None:
        """Mount something in the log.

        `replaces_splash` is what separates conversation from status. The welcome
        screen belongs to an empty conversation, so the first real turn should
        take its place — but connection notices arrive a second after launch, and
        dropping it for those meant the splash was only ever on screen for a
        blink before "Starting llama-server…" wiped it.
        """
        if replaces_splash:
            self._drop_splash()
        self.body.mount(widget)
        self.call_after_refresh(self.body.scroll_end, animate=False)

    def notice(self, text: str, error: bool = False) -> None:
        # Status, not conversation: shown under the welcome screen, not instead
        # of it.
        self.append(ChatTurn("error" if error else "notice", text), replaces_splash=False)

    def markdown(self, text: str) -> None:
        turn = ChatTurn("assistant", text)
        self.append(turn)

    def refresh_bars(self, snap: Snapshot | None = None) -> None:
        if snap is None:
            snap = self.session.snapshot()
        self.query_one("#contextbar", ContextBar).show(
            snap, self.session.pinned_pane, self.session.context_enabled
        )
        self.refresh_status()

    def refresh_status(self) -> None:
        self.query_one("#statusbar", StatusBar).show(self.session.budget)

    def on_resize(self) -> None:
        # Child widths aren't updated yet at this point; measuring now would use
        # the pre-resize width and push the meter off the right edge.
        self.call_after_refresh(self.refresh_status)

    def on_assistant_turn_grew(self, _: AssistantTurn.Grew) -> None:
        self.body.scroll_end(animate=False)

    # ---------- input ----------

    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self.query_one("#promptbar", PromptArea).clear()

        if text.startswith("/"):
            self.run_command(text)
            self.refresh_bars()
            return

        if self._streaming:
            self.notice("Still answering — press esc to stop it first.")
            return

        self.append(ChatTurn("user", text))
        self.ask(text)

    def action_clear(self) -> None:
        self.session.clear()
        for turn in list(self.query(ChatTurn)):
            turn.remove()
        # Only if one isn't already there. Clearing an already-empty conversation
        # used to mount a second splash on top of the first, and each further ^L
        # added another.
        if not self.query(Splash):
            self.body.mount(Splash(self.cfg.model.name, self.cfg.provider.key))
        self.refresh_bars()

    def action_stop(self) -> None:
        if self._streaming:
            self.workers.cancel_group(self, "chat")

    # ---------- the actual conversation ----------

    @work(exclusive=True, group="chat")
    async def ask(self, prompt: str) -> None:
        assert self._http is not None

        if not self._connected and not await self.session.client.health(self._http):
            self.notice(
                f"No llama-server at {self.cfg.provider.base_url}. Start one and try again.",
                error=True,
            )
            return
        self._connected = True

        snap = self.session.snapshot()
        messages, _ = self.session.build_messages(prompt, snap)
        self.refresh_bars(snap)
        # Exact count in the background: the estimate is already on screen, and
        # the reply matters more than the meter.
        self.count_tokens(messages)

        turn = AssistantTurn()
        self.append(turn)
        turn.start_stream()
        self._streaming = True
        cancelled = False
        error: ChatError | None = None

        try:
            async for piece in self.session.client.stream(
                self._http, messages, self.session.reply_tokens()
            ):
                turn.feed(piece)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ChatError as exc:
            # `finally` below finishes the turn; doing it here as well left a
            # removed widget being updated.
            self.notice(str(exc), error=True)
            error = exc
            return
        finally:
            self._streaming = False
            turn.finish(cancelled=cancelled)
            if turn.raw:
                # Records what the model actually wrote. The "(stopped)" marker
                # is rendered by the widget and deliberately absent from `raw`,
                # so a cancelled reply enters history as the partial text alone.
                self.session.record("user", prompt)
                self.session.record("assistant", turn.raw)
            elif error is not None:
                # Nothing arrived before it failed; an empty bubble is noise.
                turn.remove()
            self.refresh_bars(snap)

    @work(group="tokens")
    async def count_tokens(self, messages) -> None:
        """Swap the meter's estimate for llama-server's own count."""
        if self._http is None:
            return
        await self.session.refresh_budget(self._http, messages)
        self.refresh_status()

    # ---------- slash commands ----------

    def run_command(self, line: str) -> None:
        name, _, rest = line[1:].partition(" ")
        handler: Callable[[str], None] | None = self._commands().get(name.lower())
        if handler is None:
            self.notice(f"Unknown command `/{name}`. Try `/help`.", error=True)
            return
        handler(rest.strip())

    def _commands(self) -> dict[str, Callable[[str], None]]:
        return {
            "help": self.cmd_help,
            "clear": lambda _: self.action_clear(),
            "panes": self.cmd_panes,
            "pane": self.cmd_pane,
            "context": self.cmd_context,
            "model": self.cmd_model,
            "system": self.cmd_system,
            "quit": lambda _: self.exit(),
            "exit": lambda _: self.exit(),
        }

    def cmd_help(self, _: str) -> None:
        self.markdown(HELP)

    def cmd_panes(self, _: str) -> None:
        reason = tmux.unavailable_reason()
        if reason:
            self.notice(
                f"Can't see any panes — {reason}.\n"
                "Run `ask` inside a tmux pane (`tmux new`, then `ctrl-b %` to split).",
                error=True,
            )
            return
        panes = tmux.list_panes(self.cfg.context.scope)
        if not panes:
            self.notice("No other panes in scope. Split one with `ctrl-b %`.")
            return
        rows = "\n".join(
            f"| `{p.target}` | {p.command} | {p.short_path} | {p.width}x{p.height} |"
            for p in panes
        )
        self.markdown(f"| pane | cmd | path | size |\n|---|---|---|---|\n{rows}")

    def cmd_pane(self, arg: str) -> None:
        if not arg or arg in ("off", "none", "all"):
            self.session.pinned_pane = None
            self.notice("Attaching every sibling pane again.")
            return
        pane = tmux.resolve(arg)
        if pane is None:
            self.notice(f"No pane matching `{arg}`. Try `/panes`.", error=True)
            return
        self.session.pinned_pane = pane.target
        self.notice(f"Pinned to {pane.label()}")

    def cmd_context(self, arg: str) -> None:
        arg = arg.strip().lower()
        if arg in ("on", "off"):
            self.session.context_enabled = arg == "on"
            self.notice(f"Pane context {'on' if self.session.context_enabled else 'off'}.")
            return
        if arg.isdigit():
            self.cfg.context.lines = max(1, min(int(arg), 500))
            self.notice(f"Taking up to {self.cfg.context.lines} lines per pane.")
            return
        snap = self.session.snapshot()
        block = snap.render()
        if not block:
            self.notice(snap.summary())
            return
        self.markdown(f"**{snap.summary()}**\n\n```\n{block}\n```")

    def cmd_model(self, _: str) -> None:
        c = self.cfg
        self.markdown(
            f"| | |\n|---|---|\n"
            f"| model | `{c.model.name}` |\n"
            f"| file | `{c.model.id}` |\n"
            f"| endpoint | `{c.provider.base_url}` |\n"
            f"| context | {c.model.context_window} tokens |\n"
            f"| config | `{c.source}` |"
        )

    def cmd_system(self, _: str) -> None:
        origin = "models.yml" if self.cfg.system_prompt else "built-in default"
        self.markdown(f"**System prompt** ({origin})\n\n```\n{self.session.system_prompt}\n```")
