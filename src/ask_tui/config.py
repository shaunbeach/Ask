"""Load models.yml (omp's format) plus an optional sibling `ask:` block.

The models.yml file is shared with omp and is never written by us. Anything
ask-specific lives under a top-level `ask:` key that omp does not read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_CANDIDATES = (
    Path("models.yml"),
    Path.home() / ".config" / "ask" / "models.yml",
    Path.home() / ".omp" / "models.yml",
)


class ConfigError(Exception):
    pass


@dataclass
class Model:
    id: str
    name: str
    #: Where the weights actually are, when they are not in the provider's
    #: `modelDir`. Absolute (or `~`-relative) points anywhere on disk; a plain
    #: relative path is taken from `modelDir`. Kept separate from `id` because
    #: `id` is also what gets sent to the server as the model name, and a
    #: filesystem path is a poor thing to put in an API field.
    path: str | None = None
    #: Informational. Whether a model thinks is decided by its chat template and
    #: llama.cpp, not by this flag — `ask` detects reasoning from the stream
    #: either way. Parsed so the key in models.yml is reported rather than
    #: silently ignored, and shown by `/model`.
    reasoning: bool = False
    context_window: int = 8192
    max_tokens: int = 2048
    launch_args: list[str] = field(default_factory=list)


@dataclass
class Provider:
    key: str
    base_url: str
    model_dir: Path | None
    models: list[Model]


@dataclass
class ContextSettings:
    enabled: bool = True
    lines: int = 40
    max_panes: int = 4
    scope: str = "window"  # window | session
    max_tokens: int = 2000


@dataclass
class UiSettings:
    """How the app paints itself.

    `transparent` leaves the background unpainted so the terminal's own shows
    through — Ghostty's translucency, a blur, a background image. It is off-limits
    on a light terminal, though: every foreground here is a light grey chosen
    against a dark background, and on a pale one they vanish. Hence the switch.
    """

    transparent: bool = True


@dataclass
class ServerSettings:
    offer_launch: bool = True
    keep_alive: bool = False
    startup_timeout: float = 120.0


@dataclass
class Config:
    provider: Provider
    model: Model
    context: ContextSettings
    server: ServerSettings
    ui: UiSettings
    system_prompt: str | None
    reply_reserve: int = 1024
    source: Path | None = None

    @property
    def model_path(self) -> Path | None:
        """Where the weights are on disk, or None if there is no way to tell.

        Resolution order, which lets models live outside `modelDir` without
        needing a second provider:

        * `path` if the entry sets one, otherwise `id`
        * `~` expanded — pathlib does not do this, and joining an unexpanded
          `~/x` onto `modelDir` silently produced `<modelDir>/~/x`
        * absolute wins outright; relative is taken from `modelDir`
        """
        raw = self.model.path or self.model.id
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        if self.provider.model_dir is None:
            return None
        return self.provider.model_dir / candidate


def find_config(explicit: str | Path | None = None) -> Path:
    if explicit is None:
        explicit = os.environ.get("ASK_CONFIG") or None
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    for candidate in CONFIG_CANDIDATES:
        path = candidate.expanduser()
        if path.is_file():
            return path
    searched = "\n  ".join(str(c.expanduser()) for c in CONFIG_CANDIDATES)
    raise ConfigError(
        f"no models.yml found. Looked in:\n  {searched}\n"
        "Pass --config, or set ASK_CONFIG, or link your file into place:\n"
        "  mkdir -p ~/.config/ask && ln -s /path/to/models.yml ~/.config/ask/models.yml"
    )


def _parse_model(raw: dict) -> Model:
    model_id = raw.get("id")
    if not model_id:
        raise ConfigError("a model entry is missing its `id`")
    return Model(
        id=model_id,
        name=raw.get("name") or model_id,
        path=raw.get("path"),
        reasoning=bool(raw.get("reasoning", False)),
        context_window=int(raw.get("contextWindow", 8192)),
        max_tokens=int(raw.get("maxTokens", 2048)),
        launch_args=[str(a) for a in raw.get("launchArgs", [])],
    )


def _parse_provider(key: str, raw: dict) -> Provider:
    base_url = raw.get("baseUrl")
    if not base_url:
        raise ConfigError(f"provider `{key}` is missing `baseUrl`")
    model_dir = raw.get("modelDir")
    models = [_parse_model(m) for m in raw.get("models", [])]
    if not models:
        raise ConfigError(f"provider `{key}` declares no models")
    return Provider(
        key=key,
        base_url=base_url.rstrip("/"),
        model_dir=Path(model_dir).expanduser() if model_dir else None,
        models=models,
    )


def _read_system_prompt(ask: dict) -> str | None:
    prompt_file = ask.get("systemPromptFile")
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    inline = ask.get("systemPrompt")
    return inline.strip() if isinstance(inline, str) and inline.strip() else None


def load(
    explicit: str | Path | None = None,
    provider_key: str | None = None,
    model_name: str | None = None,
) -> Config:
    path = find_config(explicit)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    providers_raw = raw.get("providers") or {}
    if not providers_raw:
        raise ConfigError(f"{path} declares no `providers`")

    ask = raw.get("ask") or {}

    key = provider_key or ask.get("provider") or next(iter(providers_raw))
    if key not in providers_raw:
        available = ", ".join(providers_raw)
        raise ConfigError(f"provider `{key}` not in {path} (have: {available})")
    provider = _parse_provider(key, providers_raw[key])

    wanted = model_name or ask.get("model")
    if wanted:
        matches = [m for m in provider.models if wanted in (m.name, m.id)]
        if not matches:
            available = ", ".join(m.name for m in provider.models)
            raise ConfigError(f"model `{wanted}` not found (have: {available})")
        model = matches[0]
    else:
        model = provider.models[0]

    ctx_raw = ask.get("context") or {}
    context = ContextSettings(
        enabled=bool(ctx_raw.get("enabled", True)),
        lines=int(ctx_raw.get("lines", 40)),
        max_panes=int(ctx_raw.get("maxPanes", 4)),
        scope=str(ctx_raw.get("scope", "window")),
        max_tokens=int(ctx_raw.get("maxTokens", 2000)),
    )

    ui_raw = ask.get("ui") or {}
    ui = UiSettings(transparent=bool(ui_raw.get("transparent", True)))

    srv_raw = ask.get("server") or {}
    server = ServerSettings(
        offer_launch=bool(srv_raw.get("autoOfferLaunch", True)),
        keep_alive=bool(srv_raw.get("keepAlive", False)),
        startup_timeout=float(srv_raw.get("startupTimeout", 120.0)),
    )

    return Config(
        provider=provider,
        model=model,
        context=context,
        server=server,
        ui=ui,
        system_prompt=_read_system_prompt(ask),
        reply_reserve=int(ask.get("replyReserve", 1024)),
        source=path,
    )


if __name__ == "__main__":  # pragma: no cover - manual inspection
    cfg = load(os.environ.get("ASK_CONFIG"))
    print(f"source        {cfg.source}")
    print(f"provider      {cfg.provider.key}  {cfg.provider.base_url}")
    print(f"model         {cfg.model.name}  ({cfg.model.id})")
    print(f"model path    {cfg.model_path}")
    print(f"context win   {cfg.model.context_window}")
    print(f"launch args   {' '.join(cfg.model.launch_args)}")
    print(f"context       {cfg.context}")
    print(f"server        {cfg.server}")
    print(f"ui            {cfg.ui}")
    print(f"system prompt {'custom' if cfg.system_prompt else 'builtin default'}")
