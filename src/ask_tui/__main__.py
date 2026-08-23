"""Entry point for the `ask` command."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ask",
        description="A terminal assistant that can read your other tmux panes.",
    )
    parser.add_argument("prompt", nargs="*", help="ask once and exit, skipping the TUI")
    parser.add_argument("--config", default=None, help="path to models.yml")
    parser.add_argument("--provider", default=None, help="provider key to use")
    parser.add_argument("--model", default=None, help="model name or id to use")
    parser.add_argument(
        "--no-context", action="store_true", help="don't attach tmux pane contents"
    )
    parser.add_argument(
        "--pane", default=None, help="attach only this pane target, e.g. 2:0.1"
    )
    parser.add_argument(
        "--opaque",
        action="store_true",
        help="paint a solid background instead of letting the terminal's show through",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="print what would be sent as context, then exit",
    )
    parser.add_argument("--version", action="version", version=f"ask {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from .config import ConfigError, load

    try:
        cfg = load(args.config, provider_key=args.provider, model_name=args.model)
    except ConfigError as exc:
        print(f"ask: {exc}", file=sys.stderr)
        return 2

    if args.no_context:
        cfg.context.enabled = False
    if args.opaque:
        cfg.ui.transparent = False

    if args.show_context:
        from .session import Session

        sess = Session.create(cfg)
        sess.pinned_pane = args.pane
        snap = sess.snapshot()
        print(f"# {snap.summary()}", file=sys.stderr)
        block = snap.render()
        if block:
            print(block)
        return 0

    if args.prompt:
        from .oneshot import run

        return asyncio.run(run(cfg, " ".join(args.prompt), pinned_pane=args.pane))

    from .app import AskApp

    app = AskApp(cfg, pinned_pane=args.pane)
    app.run()
    return app.exit_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
