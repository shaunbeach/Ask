"""Streaming chat against llama-server's OpenAI-compatible endpoint.

models.yml labels the provider `api: openai-responses`, but llama-server
actually serves OpenAI *chat completions*, which is what we target here.
Sampling parameters are deliberately not sent per-request: they are already
baked into the server's launchArgs.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


class ChatError(Exception):
    pass


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LlamaClient:
    def __init__(self, base_url: str, model_id: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._timeout = timeout

    async def health(self, client: httpx.AsyncClient, timeout: float = 1.5) -> bool:
        """Is a server answering on the configured base URL?"""
        try:
            resp = await client.get(f"{self.base_url}/models", timeout=timeout)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def server_model_name(self, client: httpx.AsyncClient) -> str | None:
        """Whatever model the running server says it has loaded."""
        try:
            resp = await client.get(f"{self.base_url}/models", timeout=2.0)
            resp.raise_for_status()
            body = resp.json()
            # OpenAI spells this "data"; llama.cpp answers with "models".
            entries = body.get("data") or body.get("models") or []
            if entries:
                entry = entries[0]
                return entry.get("id") or entry.get("model") or entry.get("name")
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            pass
        return None

    async def stream(
        self,
        client: httpx.AsyncClient,
        messages: list[Message],
        max_tokens: int,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ("reasoning" | "content", text) as deltas arrive.

        A thinking model sends its scratch work as `reasoning_content`, a field
        beside `content` rather than inside it. Reading only `content` — which
        this used to do — meant every reasoning token was dropped on the floor,
        so a model that thought for twenty seconds produced twenty seconds of
        nothing and looked like a dead connection.
        """
        payload = {
            "model": self.model_id,
            "messages": [m.as_dict() for m in messages],
            "stream": True,
            "max_tokens": max_tokens,
        }
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise ChatError(_explain(resp.status_code, body))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    thinking = delta.get("reasoning_content")
                    if thinking:
                        yield ("reasoning", thinking)
                    piece = delta.get("content")
                    if piece:
                        yield ("content", piece)
        except httpx.ConnectError as exc:
            raise ChatError(
                f"could not reach llama-server at {self.base_url} — is it running?"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ChatError("llama-server stopped responding mid-reply") from exc
        except httpx.TransportError as exc:
            # Everything else the transport can raise: ReadError and
            # RemoteProtocolError are what a *killed* server looks like from
            # this end — the socket dies rather than going quiet, so neither of
            # the two cases above fires. Those escaped as bare exceptions inside
            # a Textual worker, which bypasses the `except ChatError` handlers in
            # app.py and surfaces a traceback over the conversation instead of a
            # notice. Catching the base class means a new httpx transport error
            # cannot reopen that hole.
            raise ChatError(
                f"lost the connection to llama-server ({type(exc).__name__}) — "
                "it may have been killed or run out of memory"
            ) from exc


def _explain(status: int, body: str) -> str:
    """Turn a server error body into something worth reading in a TUI."""
    detail = body.strip()
    try:
        parsed = json.loads(body)
        err = parsed.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or detail
        elif isinstance(err, str):
            detail = err
    except ValueError:
        pass
    if len(detail) > 400:
        detail = detail[:400] + "…"
    if status == 400 and "context" in detail.lower():
        return f"{detail}\n\nTry /clear, or lower /context to attach fewer lines."
    return f"llama-server returned {status}: {detail}"
