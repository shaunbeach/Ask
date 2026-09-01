#!/usr/bin/env python3
"""Tests for clipboard extraction and copying.

Plain asserts, no pytest: this build targets one machine and the venv it runs
in has exactly the three runtime dependencies. Run with:

    PYTHONPATH=src python3 tests/test_clipboard.py
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ask_tui.clipboard import Block, code_blocks, copy, detect  # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got:  {got!r}\n         want: {want!r}")


print("code_blocks")

check("no fences", code_blocks("just prose, no code"), [])

check(
    "single bash block",
    [(b.lang, b.body) for b in code_blocks("try:\n```bash\nls -la\n```\ndone")],
    [("bash", "ls -la")],
)

check(
    "two blocks keep order",
    [b.body for b in code_blocks("```sh\nfirst\n```\ntext\n```\nsecond\n```")],
    ["first", "second"],
)

check(
    "unterminated fence still yields the body",
    [b.body for b in code_blocks("```python\nprint(1)\nprint(2)")],
    ["print(1)\nprint(2)"],
)

check(
    "tilde fences",
    [b.body for b in code_blocks("~~~\necho hi\n~~~")],
    ["echo hi"],
)

check(
    "a tilde line does not close a backtick fence",
    [b.body for b in code_blocks("```\na\n~~~\nb\n```")],
    ["a\n~~~\nb"],
)

check("empty block is skipped", code_blocks("```\n```"), [])

check(
    "multi-line body preserved verbatim",
    code_blocks("```bash\ncurl -O url\ntar xzf f.tgz\n```")[0].body,
    "curl -O url\ntar xzf f.tgz",
)

check(
    "language tag with a dot",
    code_blocks("```c++\nint x;\n```")[0].lang,
    "c++",
)

print("Block.summary")
check("short body", Block("sh", "ls -la").summary(), "ls -la")
check("truncates long first line", Block("sh", "x" * 80).summary(width=10), "x" * 9 + "…")
check("empty body", Block("", "").summary(), "")

print("copy")
check("refuses empty text", copy("   ")[0], False)

tool = detect()
print(f"  info detected tool: {tool.label if tool else None}")

if tool and tool.label == "wl-copy" and shutil.which("wl-paste"):
    marker = "ask-tui-test-✓-multi\nline payload"
    ok, detail = copy(marker)
    check("wl-copy reports success", ok, True)
    got = subprocess.run(["wl-paste", "-n"], capture_output=True, text=True).stdout
    check("clipboard round-trip", got, marker)
    print(f"  info {detail}")
else:
    print("  skip live clipboard round-trip (no wl-copy/wl-paste)")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
