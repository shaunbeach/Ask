"""Token counting.

llama-server exposes /tokenize, which is exact and cheap. When it's not
reachable (server down, or we're just previewing context offline) we fall back
to a character heuristic that runs slightly pessimistic, so budgeting errs
toward sending less rather than overflowing the window.
"""

from __future__ import annotations

import httpx

CHARS_PER_TOKEN = 3.5


def estimate(text: str) -> int:
    """Offline approximation. Deliberately a slight over-count."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


class Counter:
    """Counts tokens via llama-server, degrading to the heuristic on failure."""

    def __init__(self, base_url: str) -> None:
        # `/tokenize` is llama.cpp's own endpoint, not part of the OpenAI surface,
        # so it lives beside `/v1` rather than under it. Built from `base_url`
        # unstripped this produced `/v1/tokenize`, which 404s — and because the
        # only caller was itself dead, the failure never showed up anywhere.
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        self._url = f"{root}/tokenize"
        self._exact = True

    @property
    def exact(self) -> bool:
        """False once we've fallen back to estimating."""
        return self._exact

    async def count(self, client: httpx.AsyncClient, text: str) -> int:
        if not text:
            return 0
        if self._exact:
            try:
                resp = await client.post(self._url, json={"content": text}, timeout=5.0)
                resp.raise_for_status()
                tokens = resp.json().get("tokens")
                if isinstance(tokens, list):
                    return len(tokens)
            except (httpx.HTTPError, ValueError, KeyError):
                # One failure is enough — stop paying the round-trip cost.
                self._exact = False
        return estimate(text)
