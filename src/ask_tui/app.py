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
from .clipboard import code_blocks, copy as copy_to_clipboard
from .client import ChatError, LlamaClient
from .config import Config, ConfigError, Model, load
from .context import Snapshot
from .launch_screen import LaunchScreen
from .server import ManagedServer, plan, preflight
from .files import export_name, render_transcript, strip_fences
from .files import write_file as write_file_to_disk
from .prompts import WRITE_SYSTEM_PROMPT
from .session import Budget, Session
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
| `/models` | list the models in your config |
| `/model <name>` | switch to one — the conversation is kept |
| `/model` | show the model and where it came from |
| `/write <file> <what>` | generate a file in the current directory |
| `/export [name]` | save the conversation as `YYYY-MM-DD-name.md` |
| `/think` | show the reasoning behind the last reply |
| `/copy` | copy the last code block to the clipboard |
| `/copy <n>` | copy the nth code block of the last reply |
| `/copy all` | copy the whole last reply |
| `/system` | show the active system prompt |
| `/help` | this |
| `/quit` | leave |

**Keys** — `enter` sends, `alt+enter` adds a line, `esc` stops a reply,
`alt+c` copies the last code block, `^L` clears, `^C` quits. Long questions wrap; the box grows as you type.
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
        ("alt+c", "copy", "copy code"),
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

        choice = await self.push_screen_wait(LaunchScreen(self.cfg))

        if choice.action == "quit":
            self.exit()
            return
        if choice.action == "connect" or choice.model is None:
            self.notice("Not connected — send a message once a server is up.", error=True)
            return

        # The dialog is where the model is chosen, so honour it before building
        # the launch command. Starting the first entry and making the user swap
        # afterwards meant loading two models to end up with one.
        self.adopt_model(choice.model)

        blocker = preflight(self.cfg)
        launch = plan(self.cfg)
        if blocker or launch is None:
            self.notice(f"Can't start `{self.cfg.model.name}`: {blocker or 'no launch command'}", error=True)
            return

        # Progress goes in the frame's title, not the log. Loading a model takes
        # the better part of a minute and the wait needs *something* on screen,
        # but a log line carrying an absolute path wraps over three lines in a
        # narrow pane and then sits there permanently, pushing the welcome screen
        # up. The title is one line, always visible, and cleans up after itself.
        self.set_status("starting llama-server…")
        try:
            self.managed.start(launch)
        except OSError as exc:
            self.set_status(None)
            self.notice(f"Couldn't start llama-server: {exc}", error=True)
            return

        try:
            ready = await self.managed.wait_until_ready(
                self._http, self.cfg.provider.base_url, self.cfg.server.startup_timeout
            )
        finally:
            self.set_status(None)

        if ready == "ready":
            self._connected = True
            await self._adopt_server_model()
            self.notice(f"Connected to {self.cfg.provider.base_url}")
        else:
            self._startup_failed(ready, self.managed.log_path or launch.log_path)

    def adopt_model(self, model: Model) -> None:
        """Point the app and session at `model`, leaving the conversation alone.

        Shared by the startup picker and `/model`, so the two cannot drift on
        what a switch has to update — the id in the request, the window the
        budget measures against, and the name on the splash.
        """
        self.cfg.model = model
        self.session.cfg = self.cfg
        self.session.client = LlamaClient(self.cfg.provider.base_url, model.id)
        self.session.budget = Budget(window=model.context_window)
        for splash in self.query(Splash):
            splash.refresh_labels(model.name, self.cfg.provider.key)

    def _startup_failed(self, outcome: str, log_path) -> None:
        """Explain a failed launch in terms of what actually happened.

        "died" and "timeout" need different things from the user. Reporting a
        crash as a timeout sent someone hunting for a slow disk when the real
        message — a backend that failed to load — was in the log the whole time.
        """
        tail = self.managed.tail_log(12)
        detail = f"\n\n```\n{tail}\n```" if tail else ""
        if outcome == "died":
            code = self.managed.exit_code
            headline = (
                "llama-server exited immediately"
                + (f" (exit code {code})" if code is not None else "")
                + " — it did not time out, it failed to start."
            )
        else:
            headline = (
                f"llama-server was still loading after "
                f"{int(self.cfg.server.startup_timeout)}s. It may simply be slow — "
                f"raise `server.startupTimeout` in models.yml if the log looks healthy."
            )
        self.notice(f"{headline}{detail}\n\nFull output: `{log_path}`", error=True)

    def set_status(self, text: str | None) -> None:
        """Show transient progress in the frame's border title, or clear it."""
        base = f"ask v{__version__}"
        try:
            frame = self.query_one("#frame", Vertical)
        except Exception:  # noqa: BLE001 - cosmetic; never worth failing a launch
            return
        frame.border_title = f"{base} · {text}" if text else base

    async def _adopt_server_model(self) -> None:
        """Show whatever the server actually has loaded, not just what we asked for."""
        assert self._http is not None
        name = await self.session.client.server_model_name(self._http)
        if not name:
            return
        # Only relabel when the server really is serving something else. The id
        # may carry a subfolder (`Qwen3.5-4B/Qwen3.5-4B-Q6_K.gguf`) while the
        # server reports a bare filename, and comparing the two whole made every
        # subfoldered model look like a mismatch — replacing the friendly name
        # on the splash with a raw `.gguf`.
        short = name.rsplit("/", 1)[-1]
        expected = self.cfg.model.id.rsplit("/", 1)[-1]
        if short and short != expected:
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

    def divider(self, text: str) -> None:
        """A marker in the transcript that the conversation itself never sees.

        Recorded nowhere: `session.history` holds only what the user and the
        model wrote, so a note about swapping models cannot be mistaken by the
        next model for something it said.
        """
        self.append(ChatTurn("notice", f"── {text} ──"), replaces_splash=False)

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
            async for kind, piece in self.session.client.stream(
                self._http, messages, self.session.reply_tokens()
            ):
                if kind == "reasoning":
                    turn.feed_reasoning(piece)
                else:
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

    @work(exclusive=True, group="chat")
    async def switch_model(self, wanted: str) -> None:
        """Point the session at a different model from the config.

        The conversation is kept. Clearing it on a switch would silently discard
        a thread the user may still want, and `^L` is one keystroke away if the
        handover muddies things — reversible beats tidy.
        """
        if self._streaming:
            self.notice("Still answering — press esc first.")
            return

        fresh = self._reload()
        if fresh is None:
            return
        match = next(
            (m for m in fresh.provider.models if wanted in (m.name, m.id)), None
        )
        if match is None:
            names = ", ".join(f"`{m.name}`" for m in fresh.provider.models)
            self.notice(f"No model called `{wanted}`. Have: {names}", error=True)
            return
        if match.name == self.cfg.model.name:
            self.notice(f"Already using `{match.name}`.")
            return

        # A server this app did not start belongs to someone else, and its
        # weights are already loaded. Switching the config while it keeps serving
        # the old model would make the UI say one thing and the answers be
        # another — worse than refusing.
        assert self._http is not None
        foreign = not self.managed.running and await self.session.client.health(self._http)
        if foreign:
            self.notice(
                f"The server at {self.cfg.provider.base_url} wasn't started by ask, "
                f"so its model can't be swapped from here.\n\n"
                f"Stop it and run `ask --model {match.name}`, or set "
                f"`ask.model: {match.name}` in your config.",
                error=True,
            )
            return

        previous = self.cfg.model.name
        self.cfg = fresh
        self.adopt_model(match)

        blocker = preflight(fresh)
        if blocker:
            self.notice(f"Switched to `{match.name}`, but it can't be launched: {blocker}", error=True)
            return

        launch = plan(fresh)
        if launch is None:
            self.notice(f"Switched to `{match.name}`, but no launch command could be built.", error=True)
            return

        self.set_status(f"loading {match.name}…")
        self._connected = False
        try:
            # Stops the old server first — `ManagedServer.start` replaces what it
            # is holding, and two models resident at once is what makes a load
            # crawl on a machine that cannot fit both.
            self.managed.stop()
            self.managed.start(launch)
            ready = await self.managed.wait_until_ready(
                self._http, fresh.provider.base_url, fresh.server.startup_timeout
            )
        except OSError as exc:
            self.notice(f"Couldn't start `{match.name}`: {exc}", error=True)
            return
        finally:
            self.set_status(None)

        if ready == "ready":
            self._connected = True
            self.divider(f"{previous} → {match.name}")
            self.refresh_bars()
        else:
            self._startup_failed(ready, self.managed.log_path)

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
            "models": self.cmd_models,
            "think": self.cmd_think,
            "copy": self.cmd_copy,
            "write": self.cmd_write,
            "export": self.cmd_export,
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

    def _reload(self) -> Config | None:
        """Re-read models.yml so a config edited since launch is picked up."""
        try:
            return load(self.cfg.source, provider_key=self.cfg.provider.key)
        except ConfigError as exc:
            self.notice(f"Could not re-read the config: {exc}", error=True)
            return None

    def cmd_models(self, _: str) -> None:
        fresh = self._reload() or self.cfg
        rows = []
        for m in fresh.provider.models:
            active = "●" if m.name == self.cfg.model.name else " "
            rows.append(f"| {active} | `{m.name}` | `{m.id}` | {m.context_window} |")
        body = "\n".join(rows)
        self.markdown(
            f"| | model | file | context |\n|---|---|---|---|\n{body}\n\n"
            f"`/model <name>` to switch."
        )

    def cmd_model(self, arg: str) -> None:
        if arg:
            self.switch_model(arg.strip())
            return
        c = self.cfg
        self.markdown(
            f"| | |\n|---|---|\n"
            f"| model | `{c.model.name}` |\n"
            f"| file | `{c.model.id}` |\n"
            f"| endpoint | `{c.provider.base_url}` |\n"
            f"| context | {c.model.context_window} tokens |\n"
            f"| reasoning | {'yes' if c.model.reasoning else 'no'} (per models.yml) |\n"
            f"| config | `{c.source}` |\n\n"
            f"`/models` lists the rest."
        )

    def cmd_think(self, _: str) -> None:
        """Show the scratch work behind the most recent reply.

        Kept out of the transcript by default: it is long, it is not the answer,
        and it is never sent back to the model — but it is the thing you want
        when a reply looks like it came from nowhere.
        """
        turns = [t for t in self.query(AssistantTurn) if t.reasoning]
        if not turns:
            self.notice("No reasoning recorded — the last reply didn't think, or none has run yet.")
            return
        last = turns[-1]
        took = f" ({last.thought_seconds:.1f}s)" if last.thought_seconds else ""
        self.markdown(f"**Reasoning behind the last reply{took}**\n\n```\n{last.reasoning}\n```")

    def _last_reply(self) -> str | None:
        """Raw markdown of the most recent assistant turn that said anything."""
        for turn in reversed(list(self.query(AssistantTurn))):
            if turn.raw.strip():
                return turn.raw
        return None

    def _copy(self, text: str, what: str) -> None:
        ok, detail = copy_to_clipboard(text)
        self.notice(detail if not ok else f"{what} — {detail}", error=not ok)

    def on_prompt_area_copy_requested(self, _: PromptArea.CopyRequested) -> None:
        self.action_copy()

    def action_copy(self) -> None:
        """`^Y` — the last code block, or the whole reply if it had none.

        Falling back to the whole reply rather than erroring: a one-line answer
        that the model chose not to fence is still the thing you wanted to copy,
        and being told "no code blocks" when the answer is right there reads as
        a malfunction.
        """
        reply = self._last_reply()
        if reply is None:
            self.notice("Nothing to copy yet.")
            return
        blocks = code_blocks(reply)
        if not blocks:
            self._copy(reply, "Whole reply (no fenced blocks)")
            return
        block = blocks[-1]
        label = f"`{block.summary()}`" if block.summary() else "last block"
        self._copy(block.body, f"Copied {label}")

    def cmd_copy(self, arg: str) -> None:
        """`/copy`, `/copy <n>`, `/copy all`"""
        reply = self._last_reply()
        if reply is None:
            self.notice("Nothing to copy yet.")
            return

        arg = arg.strip().lower()
        if arg in ("all", "reply"):
            self._copy(reply, "Whole reply")
            return

        blocks = code_blocks(reply)
        if not arg:
            self.action_copy()
            return
        if not blocks:
            self.notice("That reply had no fenced code blocks. `/copy all` takes the whole thing.", error=True)
            return
        if not arg.isdigit():
            self.notice(f"Usage: `/copy`, `/copy <n>` (1–{len(blocks)}), or `/copy all`.", error=True)
            return

        n = int(arg)
        if not 1 <= n <= len(blocks):
            plural = "" if len(blocks) == 1 else "s"
            self.notice(f"That reply has {len(blocks)} block{plural}; asked for {n}.", error=True)
            return
        block = blocks[n - 1]
        self._copy(block.body, f"Copied block {n}/{len(blocks)}")

    def cmd_write(self, arg: str) -> None:
        """`/write <filename> <what it should do>`"""
        name, _, brief = arg.partition(" ")
        if not name or not brief.strip():
            self.notice(
                "Usage: `/write <filename> <what it should do>`\n\n"
                "e.g. `/write backup.sh rsync ~/notes to /mnt/usb, skipping .git`",
                error=True,
            )
            return
        if self._streaming:
            self.notice("Still answering — press esc first.")
            return
        self.write_file(name, brief.strip())

    @work(exclusive=True, group="chat")
    async def write_file(self, name: str, brief: str) -> None:
        assert self._http is not None
        if not self._connected and not await self.session.client.health(self._http):
            self.notice(f"No llama-server at {self.cfg.provider.base_url}.", error=True)
            return
        self._connected = True

        self.append(ChatTurn("user", f"/write {name} — {brief}"))
        snap = self.session.snapshot()
        messages, _ = self.session.build_messages(
            f"Filename: {name}\n\nWhat it should do:\n{brief}",
            snap,
            system=WRITE_SYSTEM_PROMPT,
            reserve=self.cfg.write_reserve,
            # No history: a file is a fresh artefact, and prior chat turns both
            # crowd out the room it needs and tempt the model back into prose.
            history=False,
        )

        turn = AssistantTurn()
        self.append(turn)
        turn.start_stream()
        self._streaming = True
        cancelled = False
        try:
            async for kind, piece in self.session.client.stream(
                self._http, messages, self.session.reply_tokens(self.cfg.write_reserve)
            ):
                if kind == "reasoning":
                    turn.feed_reasoning(piece)
                else:
                    turn.feed(piece)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ChatError as exc:
            self.notice(str(exc), error=True)
            return
        finally:
            self._streaming = False
            turn.finish(cancelled=cancelled)

        body = strip_fences(turn.raw)
        if not body.strip():
            self.notice("The model returned nothing to write.", error=True)
            return
        if cancelled:
            self.notice("Stopped — nothing written.", error=True)
            return

        try:
            target = write_file_to_disk(name, body)
        except OSError as exc:
            self.notice(f"Couldn't write `{name}`: {exc}", error=True)
            return

        note = f"Wrote `{target.path}` ({len(body.splitlines())} lines)"
        if target.backup is not None:
            note += f"\n\nThe previous version is at `{target.backup.name}`."
        self.notice(note)

    def cmd_export(self, arg: str) -> None:
        """`/export [name]` — the conversation as YYYY-MM-DD-<name>.md"""
        if not self.session.history:
            self.notice("Nothing to export yet.", error=True)
            return
        name = export_name(arg.strip() or "chat")
        text = render_transcript(
            self.session.history, self.cfg.model.name, self.cfg.provider.key
        )
        try:
            target = write_file_to_disk(name, text)
        except OSError as exc:
            self.notice(f"Couldn't write `{name}`: {exc}", error=True)
            return
        turns = len(self.session.history)
        note = f"Exported {turns} message{'' if turns == 1 else 's'} to `{target.path}`"
        if target.backup is not None:
            note += f"\n\nThe previous file is at `{target.backup.name}`."
        self.notice(note)

    def cmd_system(self, _: str) -> None:
        origin = "models.yml" if self.cfg.system_prompt else "built-in default"
        self.markdown(f"**System prompt** ({origin})\n\n```\n{self.session.system_prompt}\n```")
