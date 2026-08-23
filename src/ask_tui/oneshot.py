"""Non-interactive mode: `ask "how do I ..."` prints an answer and exits.

Shares the whole session/context stack with the TUI; only the rendering differs.
"""

from __future__ import annotations

import sys

import httpx

from .client import ChatError
from .config import Config
from .session import Session


async def run(cfg: Config, prompt: str, pinned_pane: str | None = None) -> int:
    sess = Session.create(cfg)
    # Was missing, and `--pane` silently did nothing here: the TUI honoured it,
    # one-shot quietly attached every pane instead. The answer then came back
    # about whichever unrelated pane happened to be noisiest.
    sess.pinned_pane = pinned_pane
    async with httpx.AsyncClient() as http:
        if not await sess.client.health(http):
            print(
                f"No llama-server answering at {cfg.provider.base_url}.\n"
                f"Start one, or run `ask` with no arguments to be offered a launch.",
                file=sys.stderr,
            )
            return 1

        snap = sess.snapshot()
        messages, _ = sess.build_messages(prompt, snap)
        try:
            async for piece in sess.client.stream(
                http, messages, sess.reply_tokens()
            ):
                sys.stdout.write(piece)
                sys.stdout.flush()
        except ChatError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
    print()
    return 0
