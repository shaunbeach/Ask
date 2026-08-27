"""Conversation state and the context-window budget.

At 8192 tokens the budget is the whole ballgame: system prompt + pane context +
history + room to reply all have to coexist. This module owns that arithmetic
so the UI can just render the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from . import context
from .client import LlamaClient, Message
from .config import Config
from .prompts import DEFAULT_SYSTEM_PROMPT
from .tokens import Counter, estimate


@dataclass
class Turn:
    role: str
    content: str


@dataclass
class Budget:
    """A snapshot of where the context window went, for the footer meter."""

    system: int = 0
    context: int = 0
    history: int = 0
    reserve: int = 0
    window: int = 8192
    #: Exact total from llama-server's /tokenize, once it has answered. The
    #: per-part numbers stay estimates; only the figure on screen is replaced.
    counted: int | None = None
    #: False until a real count lands, so the meter can admit it is guessing.
    exact: bool = False

    @property
    def used(self) -> int:
        if self.counted is not None:
            return self.counted
        return self.system + self.context + self.history

    @property
    def total(self) -> int:
        return self.used + self.reserve

    def human(self) -> str:
        def short(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        return f"{short(self.used)}/{short(self.window)}"

    @property
    def pressure(self) -> float:
        return min(1.0, self.total / self.window) if self.window else 0.0


@dataclass
class Session:
    cfg: Config
    client: LlamaClient
    counter: Counter
    history: list[Turn] = field(default_factory=list)
    pinned_pane: str | None = None
    context_enabled: bool = True
    budget: Budget = field(default_factory=Budget)

    @classmethod
    def create(cls, cfg: Config) -> "Session":
        return cls(
            cfg=cfg,
            client=LlamaClient(cfg.provider.base_url, cfg.model.id),
            counter=Counter(cfg.provider.base_url),
            context_enabled=cfg.context.enabled,
            budget=Budget(window=cfg.model.context_window),
        )

    @property
    def system_prompt(self) -> str:
        return self.cfg.system_prompt or DEFAULT_SYSTEM_PROMPT

    def snapshot(self) -> context.Snapshot:
        """Capture panes with whatever room the budget currently allows."""
        if not self.context_enabled:
            return context.Snapshot(captures=[], unavailable="disabled (/context on)")
        return context.gather(
            self.cfg.context,
            pinned=self.pinned_pane,
            budget_tokens=self._context_allowance(),
        )

    def _context_allowance(self) -> int:
        """Tokens the pane block may occupy, after fixed costs are paid."""
        window = self.cfg.model.context_window
        fixed = estimate(self.system_prompt) + self.cfg.reply_reserve
        spare = window - fixed
        # Never let context eat more than half of what's left; the conversation
        # needs somewhere to live too.
        return max(200, min(self.cfg.context.max_tokens, spare // 2))

    def build_messages(
        self,
        prompt: str,
        snap: context.Snapshot,
        system: str | None = None,
        reserve: int | None = None,
        history: bool = True,
    ) -> tuple[list[Message], Budget]:
        """Assemble the request, evicting old turns until it fits.

        `system` and `reserve` let a command borrow the pipeline with different
        rules — `/write` needs a prompt that forbids prose and several times the
        room, without either leaking into ordinary questions.
        """
        window = self.cfg.model.context_window
        reserve = self.cfg.reply_reserve if reserve is None else reserve

        system = self.system_prompt if system is None else system
        block = snap.render()
        system_tokens = estimate(system)
        context_tokens = estimate(block)

        # The current prompt is non-negotiable; older turns are not.
        kept: list[Turn] = []
        running = estimate(prompt)
        for turn in reversed(self.history if history else []):
            cost = estimate(turn.content) + 4
            if system_tokens + context_tokens + running + cost + reserve > window:
                break
            kept.append(turn)
            running += cost
        kept.reverse()

        # Folded into the one system message rather than sent as a second
        # `system`-role message. Whether a second one is accepted is up to the
        # model's chat template: some merge them, others raise a Jinja exception
        # ("System message must be at the beginning") and the request fails with
        # a 500 — which showed up as a mid-session crash the moment pane context
        # was attached. One system message is the form every template accepts,
        # and the block is still rebuilt from scratch each turn either way.
        if block:
            system = f"{system}\n\n{block}"
        messages = [Message("system", system)]
        for turn in kept:
            messages.append(Message(turn.role, turn.content))
        messages.append(Message("user", prompt))

        budget = Budget(
            system=system_tokens,
            context=context_tokens,
            history=running,
            reserve=reserve,
            window=window,
        )
        self.budget = budget
        return messages, budget

    async def refresh_budget(
        self, http: httpx.AsyncClient, messages: list[Message]
    ) -> None:
        """Replace the estimate with an exact count from llama-server.

        One request for the whole assembled prompt rather than one per part: the
        meter shows a total, and three round-trips to break down a number nobody
        sees is not worth the latency.

        Previously this counted only the system prompt and was never called at
        all, so every figure on the meter was the chars/3.5 heuristic while the
        "~" that marks an estimate never appeared. The heuristic runs ~25% high
        on prose and ~4% *low* on terminal output — the one input this app is
        built to read — so the number was both wrong and silent about it.
        """
        joined = "\n".join(m.content for m in messages)
        try:
            total = await self.counter.count(http, joined)
        except Exception:  # noqa: BLE001 - the meter is cosmetic, never fatal
            self.budget.exact = False
            return
        if self.counter.exact:
            self.budget.counted = total
            self.budget.exact = True
        else:
            self.budget.exact = False

    def record(self, role: str, content: str) -> None:
        self.history.append(Turn(role=role, content=content))

    def clear(self) -> None:
        self.history.clear()

    def reply_tokens(self, reserve: int | None = None) -> int:
        want = self.cfg.reply_reserve if reserve is None else reserve
        return min(self.cfg.model.max_tokens, want)
