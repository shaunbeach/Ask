#!/usr/bin/env python3
"""End-to-end tests for the copy feature, driven through Textual's pilot.

These exist because the unit tests passed while the feature was broken: the
key binding was `ctrl+y`, which TextArea binds to `redo`, so it never reached
the app while the prompt had focus. Only driving the real widget tree caught
it. Run with:

    PYTHONPATH=src python3 tests/test_copy_integration.py

Requires a Wayland session with wl-clipboard; skips otherwise.
"""

import asyncio
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ask_tui.app import AskApp  # noqa: E402
from ask_tui.config import load  # noqa: E402
from ask_tui.launch_screen import LaunchScreen  # noqa: E402
from ask_tui.widgets.chat import AssistantTurn  # noqa: E402

BLOCK = "rsync -av ~/notes /mnt/usb"
REPLY = f"Do this:\n\n```bash\necho first\n```\n\nthen\n\n```bash\n{BLOCK}\n```\n"

passed = failed = 0


def clip() -> str:
    return subprocess.run(["wl-paste", "-n"], capture_output=True, text=True).stdout


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


async def _ready(pilot, app):
    """Get past the launch screen and stage one reply with two code blocks."""
    await pilot.pause()
    for _ in range(30):
        if isinstance(app.screen, LaunchScreen):
            await pilot.click("#connect")
            await pilot.pause()
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    await pilot.pause()
    turn = AssistantTurn()
    app.append(turn)
    await pilot.pause()
    turn.set_content(REPLY)
    await pilot.pause()
    app.query_one("PromptArea").focus()
    await pilot.pause()


async def run_case(setup):
    app = AskApp(load())
    async with app.run_test() as pilot:
        await _ready(pilot, app)
        result = await setup(pilot, app)
        await pilot.pause()
        await asyncio.sleep(0.4)
    return result


async def main():
    async def press_altc(pilot, app):
        focused = type(app.focused).__name__
        await pilot.press("alt+c")
        await pilot.pause()
        await asyncio.sleep(0.4)
        return focused, app.query_one("PromptArea").text

    focused, leaked = await run_case(press_altc)
    check("prompt has focus for the alt+c case", focused == "PromptArea", focused)
    check("alt+c copies the last block", clip() == BLOCK, repr(clip()))
    check("alt+c leaves no stray character in the prompt", leaked == "", repr(leaked))

    async def slash(cmd):
        async def go(pilot, app):
            app.query_one("PromptArea").text = cmd
            await pilot.press("enter")
        return go

    await run_case(await slash("/copy 1"))
    check("/copy 1 takes the first block", clip() == "echo first", repr(clip()))

    await run_case(await slash("/copy all"))
    check("/copy all takes the whole reply", clip().strip() == REPLY.strip(), repr(clip()[:40]))

    before = clip()
    await run_case(await slash("/copy 9"))
    check("/copy 9 declines without clobbering the clipboard", clip() == before)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if not (os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste")):
    print("skip: needs a Wayland session with wl-clipboard")
    sys.exit(0)

sys.exit(asyncio.run(main()))
