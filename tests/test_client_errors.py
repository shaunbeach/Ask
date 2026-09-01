#!/usr/bin/env python3
"""Every transport failure must arrive as a ChatError, never as a bare exception.

app.py catches `ChatError` around `client.stream(...)` (app.py:392, app.py:746).
Anything else escapes into a Textual worker and paints a traceback over the
conversation. `ConnectError` and `ReadTimeout` were handled; a *killed* server
raises `ReadError` or `RemoteProtocolError` instead -- the socket dies rather
than going quiet -- and those got through.

Run with:
    PYTHONPATH=src python3 tests/test_client_errors.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402

from ask_tui.client import ChatError, LlamaClient, Message  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


async def drain(exc: Exception):
    """Run stream() against a transport that raises `exc`; return what escaped."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llama = LlamaClient("http://localhost:8080/v1", "test-model")
    try:
        async for _ in llama.stream(client, [Message("user", "hi")], max_tokens=8):
            pass
    except BaseException as caught:  # noqa: BLE001 - that is the point of the test
        return caught
    finally:
        await client.aclose()
    return None


async def main():
    cases = [
        ("ConnectError", httpx.ConnectError("refused"), "could not reach"),
        ("ReadTimeout", httpx.ReadTimeout("slow"), "stopped responding"),
        # The two that previously escaped:
        ("ReadError (server killed)", httpx.ReadError("reset"), "lost the connection"),
        ("RemoteProtocolError", httpx.RemoteProtocolError("truncated"), "lost the connection"),
        ("WriteError", httpx.WriteError("broken pipe"), "lost the connection"),
        ("PoolTimeout", httpx.PoolTimeout("no conn"), "lost the connection"),
    ]

    for name, exc, fragment in cases:
        got = await drain(exc)
        check(f"{name} becomes ChatError", isinstance(got, ChatError),
              f"got {type(got).__name__}: {got}")
        if isinstance(got, ChatError):
            check(f"{name} message is useful", fragment in str(got), str(got))

    # A programming error must NOT be swallowed as a ChatError -- catching the
    # transport base class should not turn genuine bugs into polite notices.
    got = await drain(ValueError("a bug in our own code"))
    check("non-transport errors still propagate",
          isinstance(got, ValueError), f"got {type(got).__name__}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
