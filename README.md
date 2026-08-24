# ask

A terminal assistant for `llama-server` that can read your other tmux panes.

Run it in one pane, work in the others, and ask about what's on screen — a
traceback, a failed build, a directory listing — without copying anything.

```
╭─ ask v0.1.0 ───────────────────────────────────╮
│ ❯ what went wrong in my other pane?            │
│                                                │
│ There's a local math.py shadowing the standard │
│ library module, so `import random` fails.      │
│                                                │
│  mv math.py math_helpers.py                    │
│                                                │
│ ◉ 1 pane · 10 lines  (0:0.1)                   │
│ ❯                                              │
│ ^C quit · ^L clear · esc stop · /help  1.1k/8k │
╰────────────────────────────────────────────────╯
```

## Requirements

- **Python 3.11+**
- **[llama.cpp](https://github.com/ggml-org/llama.cpp)** — `llama-server` on your
  `PATH`, and a `.gguf` model
- **tmux** — optional, but it is how `ask` reads your other panes. Without it
  everything else still works; you just lose the terminal context.

## Install

No clone needed — [uv](https://docs.astral.sh/uv/) installs straight from the
repo and puts `ask` on your `PATH`:

```bash
uv tool install git+https://github.com/YOUR-USER/ask.git
```

`pipx install git+https://github.com/YOUR-USER/ask.git` works the same way.

To upgrade later, `uv tool upgrade ask-tui`. To remove it, `uv tool uninstall
ask-tui`.

### Or, to hack on it

```bash
git clone https://github.com/YOUR-USER/ask.git
cd ask
uv tool install --editable .
```

Editable, so your changes take effect without reinstalling.

## Set up a config

`ask` needs a `models.yml` pointing at your model. Copy the one in this repo and
edit the paths:

```bash
mkdir -p ~/.config/ask
curl -fsSL https://raw.githubusercontent.com/YOUR-USER/ask/main/models.yml \
  -o ~/.config/ask/models.yml
$EDITOR ~/.config/ask/models.yml
```

Set `modelDir` to the folder holding your `.gguf` files and `id` to the filename
inside it. Then run `ask` — if no server is listening it will offer to start one.

### Models in more than one place

`id` is a path *relative to* `modelDir`, so a model in a subfolder needs nothing
special. For one that lives somewhere else entirely, give it a `path`:

```yaml
models:
  - id: Qwen3-4B-Instruct-2507.gguf          # directly in modelDir
    name: qwen3-4b

  - id: Qwen3.5-4B/Qwen3.5-4B-Q6_K.gguf      # a subfolder of modelDir
    name: qwen3.5-4b

  - id: big.gguf                             # anywhere on disk
    name: big
    path: /Volumes/External/Models/big-Q4_K_M.gguf
```

`path` may be absolute, start with `~`, or be relative to `modelDir`. It is kept
separate from `id` because `id` is also what gets sent to the server as the model
name, and a filesystem path is a poor thing to put in an API field.

Pick between them with `ask --model qwen3.5-4b`, or set `ask.model` in the
config. Without either, the first entry wins.

Once running, `/models` lists them and `/model <name>` switches — `ask` stops the
llama-server it started and brings the new model up in its place. Your
conversation is kept across the switch (`^L` clears it if you'd rather start
fresh). A server `ask` did not start is left alone: its weights are already
loaded, so `ask` tells you to relaunch rather than let the UI claim one model
while the answers come from another.

## Use

```bash
ask                       # the TUI
ask "how do I grep recursively"   # one-shot, prints and exits
ask --show-context        # print exactly what would be sent, then exit
ask --pane 2:0.1 "..."    # attach only this pane
ask --no-context "..."    # attach nothing
ask --opaque              # paint a solid background instead of the terminal's
```

Split a pane with `ctrl-b %`, run `ask` in one of them, and it picks up the
rest of the window automatically.

## What it sends

Every message carries the recent contents of your other panes in the current
tmux window. The bar above the input always shows what's attached — nothing is
sent invisibly.

Before sending, each pane is cleaned up: wrapped lines are rejoined, blank runs
and repeated shell prompts are collapsed, and panes showing nothing but an idle
prompt are dropped. What's left is trimmed to fit the context window.

## Selections

If you have text highlighted in another pane — a visual-mode selection in vim, a
region in a pager — `ask` notices and tells the model that is what you are
asking about. So "what does this do?" works without naming anything.

It reads this from colour, not from text: a selection is a background colour
painted behind the region, so the capture keeps the escape codes and looks for
runs whose background differs from the rest of the pane. Syntax highlighting
changes the *foreground* and is ignored; `hlsearch` paints every match of the
same string, and repeated identical runs are treated as a search rather than a
selection.

One case is genuinely ambiguous: a single fully-highlighted line looks the same
whether you selected it with `V` or vim's `cursorline` is simply painting where
the cursor sits. That is reported as "the cursor is on line N" rather than as a
selection, which is true either way.

The context bar shows `✦ selection` when one is attached.

Outside tmux there is no supported way to read another terminal, so the context
bar reads `context unavailable` and `ask` works as a plain chat client. If a
tmux server is running but `ask` isn't inside it, `/panes` still lists
everything and `/pane <target>` attaches one.

## Commands

| | |
|---|---|
| `/panes` | list panes it can see |
| `/pane <target>` | attach only that pane, e.g. `/pane 2:0.1` |
| `/pane off` | go back to attaching every sibling pane |
| `/context on\|off` | toggle pane context |
| `/context <n>` | lines to take from each pane |
| `/context` | show the exact block that would be sent |
| `/clear` | forget the conversation |
| `/models` | list the models in your config, active one marked |
| `/model <name>` | switch models without restarting |
| `/model` | model, endpoint, config path |
| `/think` | the reasoning behind the last reply |
| `/system` | the active system prompt |
| `/help`, `/quit` | |

Keys: `enter` sends, `alt+enter` (or `ctrl+j`) adds a line without sending,
`esc` stops a reply, `^L` clears, `^C` quits.

The input wraps and grows as you type, so a long question stays fully visible
in a narrow pane instead of scrolling off the edge.

## Configuration

`ask` looks for `models.yml` at `--config`, then `$ASK_CONFIG`, then
`./models.yml`, `~/.config/ask/models.yml`, and `~/.omp/models.yml` — first one
found wins. It never writes to it.

`~/.config/ask/models.yml` is the one to use if you want `ask` to work from any
directory. If you already keep one for [omp](https://github.com/ggml-org/llama.cpp)
the format is the same file, so you can point at that instead:

```bash
ln -s ~/.omp/models.yml ~/.config/ask/models.yml
```

Anything ask-specific goes under a top-level `ask:` key, which omp does not
read. Every field is optional:

```yaml
ask:
  model: your-model            # matches a `name:` above; default is the first
  systemPrompt: |              # or systemPromptFile: ~/.config/ask/system.md
    You are a terminal assistant...
  context:
    enabled: true
    lines: 40                  # scrollback lines per pane
    maxPanes: 4
    scope: window              # window | session | all
    maxTokens: 2000            # cap on the pane block
  server:
    autoOfferLaunch: true      # offer to start llama-server when none answers
    keepAlive: false           # leave it running after ask exits
  ui:
    transparent: true          # let the terminal's own background show through
  replyReserve: 1024           # tokens held back for the reply
```

## Background

By default `ask` paints no background of its own, so whatever the terminal is
showing comes through — Ghostty's translucency, a blur, a background image. It
does this by resolving every surface to `ansi_default`, which emits no colour
code at all, rather than by guessing at your theme's colour and hoping it
matches.

The foreground colours are all light, chosen against a dark terminal. On a light
background they will be hard to read, so `ask --opaque` (or `ui.transparent:
false`) paints the dark surface back in.

## The server

On startup `ask` probes the configured `baseUrl`. If nothing answers, it lists
the models in your config and waits for you to pick one — arrows to move, enter
to start it. The model you choose is the one that gets loaded, so you never pay
to load one model and then swap to another. A model whose file has moved is
shown as `missing` rather than failing a minute into a load.

With only one model configured there is nothing to choose, so it just offers to
start it. It never spawns a
model behind your back. A server `ask` started is shut down when `ask` exits,
unless `keepAlive` is set. Its output goes to
`~/.local/state/ask/llama-server.log`.

## Thinking models

llama.cpp sends a reasoning model's scratch work as `reasoning_content`, a field
separate from the answer. `ask` reads it and shows a live counter while it runs,
so a model that thinks for twenty seconds doesn't look like a dead connection:

```
⋯ thinking… 4.7s          while it works
⋯ thought for 6.2s        once the answer starts
```

`/think` prints the reasoning behind the last reply. It is kept out of the
transcript by default — it's long, it isn't the answer, and it is **never** sent
back to the model, so thinking from one turn cannot eat the context window on
the next.

Detection is automatic. The `reasoning:` key in `models.yml` is informational
only — whether a model thinks is decided by its chat template and llama.cpp, not
by that flag — and `/model` reports what it says.

## Context budget

At an 8192-token window the budget is the whole game, so `ask` accounts for it
explicitly: the system prompt and a reply reserve come off the top, the pane
block is capped at half of what's left, and the conversation gets the rest with
oldest turns evicted first. Token counts come from llama-server's `/tokenize`,
falling back to an estimate (shown as `~`) if that's unreachable. The meter in
the footer turns amber past 70% and red past 90%.

## Development

```bash
uv venv && uv pip install -e .
.venv/bin/python -m ask_tui                 # run from source
.venv/bin/python -m ask_tui.context --dump  # see the context block only
.venv/bin/ruff check src/
```

`models.yml` in this repo is a **generic template** and is committed as such.
Keep your real paths in `~/.config/ask/models.yml` (or `models_bak.yml`, which
`.gitignore` excludes) so they never end up in a commit.
