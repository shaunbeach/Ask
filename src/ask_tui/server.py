"""llama-server lifecycle.

We never spawn a server behind the user's back — loading a model pins GPU
memory for a while, and that should always be a deliberate choice. The app
probes, then offers.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config, xdg_dir

# Honours $XDG_STATE_HOME where it is set; the fallback is the spec's default
# and what macOS ends up using.
LOG_DIR = xdg_dir("XDG_STATE_HOME", ".local/state") / "ask"


def binary() -> str | None:
    return shutil.which("llama-server")


@dataclass
class LaunchPlan:
    """Everything needed to start the server, and to show the user first."""

    argv: list[str]
    model_path: Path
    log_path: Path

    def command_line(self) -> str:
        return " ".join(_quote(a) for a in self.argv)


def _quote(arg: str) -> str:
    return f"'{arg}'" if any(c in arg for c in " ;|&$<>()") else arg


def plan(cfg: Config) -> LaunchPlan | None:
    """Build the launch command from models.yml, or None if we can't."""
    exe = binary()
    model_path = cfg.model_path
    if exe is None or model_path is None:
        return None
    argv = [exe, "-m", str(model_path), *cfg.model.launch_args]
    return LaunchPlan(
        argv=argv, model_path=model_path, log_path=LOG_DIR / "llama-server.log"
    )


def preflight(cfg: Config) -> str | None:
    """A human-readable reason we couldn't launch, or None if we can."""
    if binary() is None:
        return "llama-server is not on your PATH"
    model_path = cfg.model_path
    if model_path is None:
        return f"provider `{cfg.provider.key}` has no modelDir, so the GGUF can't be located"
    if not model_path.is_file():
        return f"model file not found: {model_path}"
    return None


class ManagedServer:
    """A llama-server process we started, and are therefore responsible for."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen | None = None
        self._log: Path | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def log_path(self) -> Path | None:
        return self._log

    def start(self, launch: LaunchPlan) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._log = launch.log_path
        # Closed straight after the spawn: the child gets its own descriptor, and
        # holding the parent's copy open leaks one per restart.
        with open(launch.log_path, "w", encoding="utf-8") as handle:
            self._proc = subprocess.Popen(
                launch.argv,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Own process group, so a Ctrl-C in our terminal doesn't race us
                # to kill the server before we can shut it down cleanly.
                start_new_session=True,
            )

    def tail_log(self, lines: int = 15) -> str:
        if self._log is None or not self._log.is_file():
            return ""
        try:
            content = self._log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def died(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    @property
    def exit_code(self) -> int | None:
        """The code the server exited with, if it has."""
        return None if self._proc is None else self._proc.poll()

    async def wait_until_ready(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        timeout: float,
        on_tick=None,
    ) -> str:
        """Poll /models until the model loads. Returns "ready", "died" or "timeout".

        The three are worth telling apart. A server that exits after two seconds
        and one that is still loading after two minutes need completely different
        things from the user, and reporting both as a timeout sent someone
        hunting for a slow disk when the real message — a backend that failed to
        load — was sitting in the log the whole time.
        """
        url = f"{base_url.rstrip('/')}/models"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.died():
                return "died"
            try:
                resp = await client.get(url, timeout=2.0)
                if resp.status_code < 500:
                    return "ready"
            except httpx.HTTPError:
                pass
            if on_tick is not None:
                on_tick(max(0.0, deadline - loop.time()))
            await asyncio.sleep(0.6)
        return "timeout"

    def stop(self) -> None:
        """Shut down only if we started it and weren't told to keep it alive."""
        if self._proc is None or self._cfg.server.keep_alive:
            return
        if self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.terminate()
            except OSError:
                return
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except OSError:
                pass
